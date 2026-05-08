from __future__ import annotations

import re
from datetime import date, datetime, time
from decimal import Decimal
from calendar import monthrange
from os.path import basename
from uuid import uuid4

from django.core.files.storage import default_storage
from django.core.files.base import ContentFile
from django.db import transaction
from django.db.models import (
    Count,
    DateTimeField,
    DecimalField,
    Max,
    OuterRef,
    Prefetch,
    Q,
    Subquery,
    Sum,
    Value,
)
from django.db.models.functions import Coalesce
from django.utils.text import slugify
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.accounts.models import User
from apps.associados.models import Associado
from apps.contratos.canonicalization import (
    get_operational_contracts_for_associado,
    operational_contracts_queryset,
)
from apps.contratos.competencia import (
    create_cycle_with_parcelas,
    list_month_references,
    parcela_has_financial_evidence,
    propagate_competencia_status,
    sync_competencia_locks_for_references,
)
from apps.contratos.cycle_rebuild import rebuild_contract_cycle_state
from apps.contratos.models import Ciclo, Contrato, Parcela
from apps.contratos.soft_delete import soft_delete_contract_tree
from apps.esteira.models import EsteiraItem, Transicao
from apps.financeiro.models import Despesa
from apps.importacao.models import ArquivoRetorno, ArquivoRetornoItem, PagamentoMensalidade
from apps.importacao.return_auto_enrollment import build_payment_identity
from apps.refinanciamento.models import Comprovante, Refinanciamento
from apps.tesouraria.initial_payment import get_initial_payment_for_contract

from .models import BaixaManual, Confirmacao, DevolucaoAssociado, Pagamento


def parse_competencia(value: str | None) -> date:
    if not value:
        today = timezone.localdate()
        return today.replace(day=1)
    try:
        parsed = datetime.strptime(value, "%Y-%m").date()
    except ValueError as exc:
        raise ValidationError("Competência inválida. Use o formato YYYY-MM.") from exc
    return parsed.replace(day=1)


def shift_month(base_date: date, offset: int) -> date:
    month_index = base_date.month - 1 + offset
    year = base_date.year + month_index // 12
    month = month_index % 12 + 1
    day = min(base_date.day, monthrange(year, month)[1])
    return date(year, month, day)


class TesourariaService:
    @staticmethod
    def dispensa_pagamento_inicial(contrato: Contrato) -> bool:
        return Decimal(str(contrato.valor_mensalidade or Decimal("0.00"))) <= Decimal("0.00")

    @staticmethod
    def listar_usuarios_filtro_agente() -> list[dict[str, object]]:
        linked_queryset = Contrato.objects.filter(
            Q(
                associado__esteira_item__etapa_atual__in=[
                    EsteiraItem.Etapa.TESOURARIA,
                    EsteiraItem.Etapa.CONCLUIDO,
                ]
            )
            | Q(status__in=[Contrato.Status.CANCELADO, Contrato.Status.ENCERRADO])
        )
        linked_user_ids = {
            user_id
            for user_id in (
                list(linked_queryset.values_list("agente_id", flat=True))
                + list(
                    linked_queryset.values_list(
                        "associado__agente_responsavel_id",
                        flat=True,
                    )
                )
            )
            if user_id
        }

        users = (
            User.objects.filter(
                Q(
                    is_active=True,
                    user_roles__deleted_at__isnull=True,
                    user_roles__role__codigo="AGENTE",
                )
                | Q(id__in=linked_user_ids)
            )
            .distinct()
            .order_by("first_name", "last_name", "email")
        )
        return [
            {
                "id": user.id,
                "full_name": user.full_name or user.email,
                "email": user.email,
                "primary_role": user.primary_role,
            }
            for user in users
        ]

    @staticmethod
    def listar_contratos_pendentes(
        competencia: date | None = None,
        data_inicio: str | None = None,
        data_fim: str | None = None,
        data_anexo_associado_inicio: str | None = None,
        data_anexo_associado_fim: str | None = None,
        data_anexo_agente_inicio: str | None = None,
        data_anexo_agente_fim: str | None = None,
        data_pagamento_associado_inicio: str | None = None,
        data_pagamento_associado_fim: str | None = None,
        data_pagamento_agente_inicio: str | None = None,
        data_pagamento_agente_fim: str | None = None,
        search: str | None = None,
        pagamento: str | None = None,
        agente: str | None = None,
        status_contrato: str | None = None,
        situacao_esteira: str | None = None,
        origem_operacional: str | None = None,
        ordering: str | None = None,
    ):
        ordering_map = {
            "created_at": "created_at",
            "-created_at": "-created_at",
            "data_contrato": "data_contrato",
            "-data_contrato": "-data_contrato",
            "codigo": "codigo",
            "-codigo": "-codigo",
        }
        has_explicit_period = bool(data_inicio or data_fim)
        queryset = (
            Contrato.objects.select_related(
                "associado",
                "associado__dados_bancarios",
                "associado__esteira_item",
                "agente",
            )
            .prefetch_related(Prefetch("comprovantes__enviado_por"), Prefetch("ciclos"))
            .filter(
                # Itens ativos na fila ou já efetivados/processados.
                # Exclui CONCLUIDO+REJEITADO do filtro de etapa: esses são
                # exclusões operacionais preservadas (não efetivações) e não
                # devem aparecer na lista ativa. Contratos cancelados (status=CANCELADO)
                # continuam visíveis pela segunda condição independentemente.
                (
                    Q(associado__esteira_item__etapa_atual__in=[
                        EsteiraItem.Etapa.TESOURARIA,
                        EsteiraItem.Etapa.CONCLUIDO,
                    ])
                    & Q(associado__esteira_item__deleted_at__isnull=True)
                    & ~Q(
                        associado__esteira_item__etapa_atual=EsteiraItem.Etapa.CONCLUIDO,
                        associado__esteira_item__status=EsteiraItem.Situacao.REJEITADO,
                    )
                )
                | (
                    Q(status__in=[Contrato.Status.CANCELADO, Contrato.Status.ENCERRADO])
                    & Q(associado__esteira_item__deleted_at__isnull=True)
                )
            )
            .distinct()
            .order_by(ordering_map.get(ordering or "", "-created_at"))
        )
        queryset = operational_contracts_queryset(queryset)
        comprovantes_queryset = Comprovante.objects.filter(
            contrato_id=OuterRef("pk"),
            refinanciamento__isnull=True,
            deleted_at__isnull=True,
        )
        associado_comprovante = comprovantes_queryset.filter(
            papel=Comprovante.Papel.ASSOCIADO,
            tipo=Comprovante.Tipo.COMPROVANTE_PAGAMENTO_ASSOCIADO,
        ).order_by("-updated_at", "-created_at", "-id")
        agente_comprovante = comprovantes_queryset.filter(
            papel=Comprovante.Papel.AGENTE,
            tipo=Comprovante.Tipo.COMPROVANTE_PAGAMENTO_AGENTE,
        ).order_by("-updated_at", "-created_at", "-id")
        queryset = queryset.annotate(
            data_anexo_associado=Subquery(
                associado_comprovante.values("updated_at")[:1],
                output_field=DateTimeField(),
            ),
            data_anexo_agente=Subquery(
                agente_comprovante.values("updated_at")[:1],
                output_field=DateTimeField(),
            ),
            data_pagamento_associado=Subquery(
                associado_comprovante.values("data_pagamento")[:1],
                output_field=DateTimeField(),
            ),
            data_pagamento_agente=Subquery(
                agente_comprovante.values("data_pagamento")[:1],
                output_field=DateTimeField(),
            ),
        )

        if pagamento == "pendente":
            queryset = queryset.filter(
                associado__esteira_item__etapa_atual=EsteiraItem.Etapa.TESOURARIA
            ).filter(auxilio_liberado_em__isnull=True)
        elif pagamento == "concluido":
            queryset = queryset.filter(
                Q(associado__esteira_item__etapa_atual=EsteiraItem.Etapa.CONCLUIDO)
                | Q(auxilio_liberado_em__isnull=False)
            ).exclude(status__in=[Contrato.Status.CANCELADO, Contrato.Status.ENCERRADO])
        elif pagamento == "liquidado":
            queryset = queryset.filter(status=Contrato.Status.ENCERRADO)
        elif pagamento == "cancelado":
            queryset = queryset.filter(status=Contrato.Status.CANCELADO)
        elif pagamento == "processado":
            queryset = queryset.exclude(
                associado__esteira_item__etapa_atual=EsteiraItem.Etapa.TESOURARIA
            )

        if competencia and not has_explicit_period:
            if pagamento == "concluido":
                queryset = queryset.filter(
                    auxilio_liberado_em__year=competencia.year,
                    auxilio_liberado_em__month=competencia.month,
                )
            elif pagamento == "liquidado":
                queryset = queryset.filter(
                    updated_at__year=competencia.year,
                    updated_at__month=competencia.month,
                )
            elif pagamento == "cancelado":
                queryset = queryset.filter(
                    cancelado_em__year=competencia.year,
                    cancelado_em__month=competencia.month,
                )
            elif pagamento == "processado":
                queryset = queryset.filter(
                    Q(
                        auxilio_liberado_em__year=competencia.year,
                        auxilio_liberado_em__month=competencia.month,
                    )
                    | Q(
                        status__in=[Contrato.Status.CANCELADO, Contrato.Status.ENCERRADO],
                        updated_at__year=competencia.year,
                        updated_at__month=competencia.month,
                    )
                )

        if data_inicio:
            queryset = queryset.filter(created_at__date__gte=data_inicio)

        if data_fim:
            queryset = queryset.filter(created_at__date__lte=data_fim)

        if data_anexo_associado_inicio:
            queryset = queryset.filter(
                data_anexo_associado__date__gte=data_anexo_associado_inicio
            )
        if data_anexo_associado_fim:
            queryset = queryset.filter(
                data_anexo_associado__date__lte=data_anexo_associado_fim
            )
        if data_anexo_agente_inicio:
            queryset = queryset.filter(
                data_anexo_agente__date__gte=data_anexo_agente_inicio
            )
        if data_anexo_agente_fim:
            queryset = queryset.filter(
                data_anexo_agente__date__lte=data_anexo_agente_fim
            )
        if data_pagamento_associado_inicio:
            queryset = queryset.filter(
                data_pagamento_associado__date__gte=data_pagamento_associado_inicio
            )
        if data_pagamento_associado_fim:
            queryset = queryset.filter(
                data_pagamento_associado__date__lte=data_pagamento_associado_fim
            )
        if data_pagamento_agente_inicio:
            queryset = queryset.filter(
                data_pagamento_agente__date__gte=data_pagamento_agente_inicio
            )
        if data_pagamento_agente_fim:
            queryset = queryset.filter(
                data_pagamento_agente__date__lte=data_pagamento_agente_fim
            )

        if search:
            queryset = queryset.filter(
                Q(associado__nome_completo__icontains=search)
                | Q(associado__cpf_cnpj__icontains=search)
                | Q(codigo__icontains=search)
                | Q(associado__matricula__icontains=search)
            )

        if agente:
            agente_term = agente.strip()
            if agente_term:
                if agente_term.isdigit():
                    agente_id = int(agente_term)
                    queryset = queryset.filter(
                        Q(agente_id=agente_id)
                        | Q(associado__agente_responsavel_id=agente_id)
                    )
                else:
                    queryset = queryset.filter(
                        Q(agente__first_name__icontains=agente_term)
                        | Q(agente__last_name__icontains=agente_term)
                        | Q(agente__email__icontains=agente_term)
                        | Q(
                            associado__agente_responsavel__first_name__icontains=agente_term
                        )
                        | Q(
                            associado__agente_responsavel__last_name__icontains=agente_term
                        )
                        | Q(
                            associado__agente_responsavel__email__icontains=agente_term
                        )
                    )

        if status_contrato == "congelado":
            queryset = queryset.filter(
                associado__esteira_item__status=EsteiraItem.Situacao.PENDENCIADO
            )
        elif status_contrato and status_contrato in {
            choice[0] for choice in Contrato.Status.choices
        }:
            queryset = queryset.filter(status=status_contrato)

        if situacao_esteira and situacao_esteira in {
            choice[0] for choice in EsteiraItem.Situacao.choices
        }:
            queryset = queryset.filter(associado__esteira_item__status=situacao_esteira)

        if origem_operacional and origem_operacional in {
            choice[0] for choice in Contrato.OrigemOperacional.choices
        }:
            queryset = queryset.filter(origem_operacional=origem_operacional)
            if origem_operacional == Contrato.OrigemOperacional.REATIVACAO:
                # Na secao de reativacoes exibe apenas contratos pendentes,
                # nao reativacoes antigas ja canceladas/encerradas.
                queryset = queryset.exclude(
                    status__in=[Contrato.Status.CANCELADO, Contrato.Status.ENCERRADO]
                )

        return queryset

    @staticmethod
    def _get_contrato(contrato_id: int) -> Contrato:
        try:
            return (
                Contrato.objects.select_related(
                    "associado",
                    "associado__dados_bancarios",
                    "associado__esteira_item",
                    "agente",
                )
                .prefetch_related("ciclos__parcelas", "comprovantes")
                .get(pk=contrato_id)
            )
        except Contrato.DoesNotExist as exc:
            raise ValidationError("Contrato não encontrado.") from exc

    @staticmethod
    def _restore_associado_status_after_pending_reativacao(contrato: Contrato) -> None:
        if contrato.origem_operacional != Contrato.OrigemOperacional.REATIVACAO:
            return

        associado = contrato.associado
        possui_outro_contrato_operacional = associado.contratos.filter(
            deleted_at__isnull=True,
            contrato_canonico__isnull=True,
        ).exclude(pk=contrato.pk).exclude(
            status__in=[Contrato.Status.CANCELADO, Contrato.Status.ENCERRADO]
        ).exists()
        if possui_outro_contrato_operacional:
            return

        Associado.objects.filter(pk=associado.pk).update(
            status=Associado.Status.INATIVO,
            updated_at=timezone.now(),
        )

    @staticmethod
    def _is_pending_reactivation_contract(contrato: Contrato) -> bool:
        return bool(
            contrato.origem_operacional == Contrato.OrigemOperacional.REATIVACAO
            and contrato.auxilio_liberado_em is None
            and contrato.status
            not in {
                Contrato.Status.ATIVO,
                Contrato.Status.CANCELADO,
                Contrato.Status.ENCERRADO,
            }
        )

    @staticmethod
    def _has_remaining_open_operational_contract(contrato: Contrato) -> bool:
        return (
            contrato.associado.contratos.filter(
                deleted_at__isnull=True,
                contrato_canonico__isnull=True,
            )
            .exclude(pk=contrato.pk)
            .exclude(status__in=[Contrato.Status.CANCELADO, Contrato.Status.ENCERRADO])
            .exists()
        )

    @staticmethod
    def _registrar_remocao_contrato_sem_fechar_esteira(
        esteira_item: EsteiraItem,
        *,
        user,
        observacao: str,
    ) -> None:
        Transicao.objects.create(
            esteira_item=esteira_item,
            acao="remover_fila_operacional",
            de_status=esteira_item.etapa_atual,
            para_status=esteira_item.etapa_atual,
            de_situacao=esteira_item.status,
            para_situacao=esteira_item.status,
            realizado_por=user,
            observacao=observacao,
        )

    @staticmethod
    def _day_of_month(month_date: date, target_day: int = 5) -> date:
        return month_date.replace(
            day=min(target_day, monthrange(month_date.year, month_date.month)[1])
        )

    @staticmethod
    def _latest_paid_parcela_for_reactivation(contrato: Contrato) -> Parcela | None:
        return (
            Parcela.objects.select_related("ciclo", "ciclo__contrato")
            .filter(
                associado_id=contrato.associado_id,
                status__in=[Parcela.Status.DESCONTADO, Parcela.Status.LIQUIDADA],
            )
            .exclude(ciclo__contrato_id=contrato.id)
            .order_by("-referencia_mes", "-data_pagamento", "-updated_at", "-id")
            .first()
        )

    @staticmethod
    def build_reactivation_cycle_preview(contrato: Contrato) -> dict[str, object] | None:
        if contrato.origem_operacional != Contrato.OrigemOperacional.REATIVACAO:
            return None

        last_paid = TesourariaService._latest_paid_parcela_for_reactivation(contrato)
        if last_paid is not None:
            competencia_inicial = shift_month(last_paid.referencia_mes.replace(day=1), 1)
        elif contrato.data_primeira_mensalidade:
            competencia_inicial = contrato.data_primeira_mensalidade.replace(day=1)
        else:
            competencia_inicial = timezone.localdate().replace(day=1)

        parcelas_total = int(contrato.prazo_meses or 3)
        competencias = list_month_references(competencia_inicial, parcelas_total)
        return {
            "ultima_parcela_paga": last_paid.referencia_mes.isoformat()
            if last_paid
            else None,
            "competencia_inicial_sugerida": competencia_inicial.isoformat(),
            "competencias_sugeridas": [
                competencia.isoformat() for competencia in competencias
            ],
            "parcelas_total": parcelas_total,
        }

    @staticmethod
    def _normalize_reactivation_competencias(
        contrato: Contrato,
        competencias_ciclo,
    ) -> list[date]:
        if contrato.origem_operacional != Contrato.OrigemOperacional.REATIVACAO:
            return []
        if not competencias_ciclo:
            preview = TesourariaService.build_reactivation_cycle_preview(contrato)
            competencias_ciclo = (
                preview.get("competencias_sugeridas", []) if preview else []
            )
        if not competencias_ciclo:
            raise ValidationError(
                {
                    "competencias_ciclo": [
                        "Não foi possível calcular as parcelas do ciclo da reativação."
                    ]
                }
            )

        competencias = sorted(
            {
                (
                    datetime.strptime(item, "%Y-%m-%d").date()
                    if isinstance(item, str)
                    else item
                ).replace(day=1)
                for item in competencias_ciclo
            }
        )
        parcelas_total = int(contrato.prazo_meses or 3)
        if len(competencias) != parcelas_total:
            raise ValidationError(
                {
                    "competencias_ciclo": [
                        f"Confirme exatamente {parcelas_total} parcela(s) para o ciclo da reativação."
                    ]
                }
            )

        expected = list_month_references(competencias[0], parcelas_total)
        if competencias != expected:
            raise ValidationError(
                {
                    "competencias_ciclo": [
                        "As competências do ciclo da reativação devem ser meses consecutivos."
                    ]
                }
            )
        return competencias

    @staticmethod
    def _prepare_reactivation_cycle_effectivation(
        contrato: Contrato,
        *,
        competencias: list[date],
    ) -> Ciclo | None:
        if contrato.origem_operacional != Contrato.OrigemOperacional.REATIVACAO:
            return None

        if contrato.ciclos.filter(deleted_at__isnull=True).exists():
            raise ValidationError(
                "Este contrato de reativação já possui ciclo materializado."
            )

        last_paid = TesourariaService._latest_paid_parcela_for_reactivation(contrato)
        if last_paid is not None and last_paid.ciclo_id:
            last_cycle = last_paid.ciclo
            if last_cycle.status != Ciclo.Status.FECHADO:
                last_cycle.status = Ciclo.Status.FECHADO
                last_cycle.save(update_fields=["status", "updated_at"])

        conflicts = (
            Parcela.objects.select_related("ciclo", "ciclo__contrato")
            .filter(
                associado_id=contrato.associado_id,
                referencia_mes__in=competencias,
            )
            .exclude(ciclo__contrato_id=contrato.id)
            .order_by("referencia_mes", "id")
        )
        references_to_sync = set(competencias)
        for parcela in conflicts:
            if parcela_has_financial_evidence(parcela):
                references_to_sync.add(parcela.referencia_mes)
                continue
            parcela.status = Parcela.Status.CANCELADO
            parcela.observacao = (
                (parcela.observacao or "").strip()
                + "\nParcela cancelada para materialização da reativação."
            ).strip()
            parcela.save(update_fields=["status", "observacao", "updated_at"])
            parcela.soft_delete()
            references_to_sync.add(parcela.referencia_mes)

        ciclo, _parcelas = create_cycle_with_parcelas(
            contrato=contrato,
            numero=1,
            competencia_inicial=competencias[0],
            parcelas_total=len(competencias),
            ciclo_status=Ciclo.Status.ABERTO,
            parcela_status=Parcela.Status.EM_PREVISAO,
            data_vencimento_fn=TesourariaService._day_of_month,
            valor_mensalidade=contrato.valor_mensalidade,
            valor_total=contrato.valor_total_antecipacao,
        )
        sync_competencia_locks_for_references(
            associado_id=contrato.associado_id,
            referencias=sorted(references_to_sync),
        )
        return ciclo

    @staticmethod
    def _payment_tipo_for_papel(papel: str) -> str:
        return (
            Comprovante.Tipo.COMPROVANTE_PAGAMENTO_ASSOCIADO
            if papel == Comprovante.Papel.ASSOCIADO
            else Comprovante.Tipo.COMPROVANTE_PAGAMENTO_AGENTE
        )

    @staticmethod
    def _latest_contract_payment_comprovante(
        contrato: Contrato,
        papel: str,
    ) -> Comprovante | None:
        return (
            contrato.comprovantes.filter(
                refinanciamento__isnull=True,
                deleted_at__isnull=True,
                papel=papel,
                tipo=TesourariaService._payment_tipo_for_papel(papel),
            )
            .order_by("-updated_at", "-created_at", "-id")
            .first()
        )

    @staticmethod
    def _upsert_contract_payment_comprovante(
        contrato: Contrato,
        *,
        papel: str,
        arquivo,
        user,
        data_pagamento: datetime,
        ciclo=None,
    ) -> Comprovante:
        payload = {
            "contrato": contrato,
            "ciclo": ciclo,
            "refinanciamento": None,
            "papel": papel,
            "tipo": TesourariaService._payment_tipo_for_papel(papel),
            "origem": Comprovante.Origem.EFETIVACAO_CONTRATO,
            "arquivo": arquivo,
            "nome_original": getattr(arquivo, "name", ""),
            "mime": getattr(arquivo, "content_type", "") or "",
            "size_bytes": getattr(arquivo, "size", None),
            "arquivo_referencia_path": "",
            "enviado_por": user,
            "data_pagamento": data_pagamento,
            "agente_snapshot": contrato.agente.full_name if contrato.agente else "",
        }
        return Comprovante.objects.create(**payload)

    @staticmethod
    def _sync_initial_payment_paths(pagamento: Pagamento, contrato: Contrato) -> Pagamento:
        comprovante_associado = TesourariaService._latest_contract_payment_comprovante(
            contrato,
            Comprovante.Papel.ASSOCIADO,
        )
        comprovante_agente = TesourariaService._latest_contract_payment_comprovante(
            contrato,
            Comprovante.Papel.AGENTE,
        )
        pagamento.comprovante_associado_path = (
            comprovante_associado.arquivo_referencia if comprovante_associado else ""
        )
        pagamento.comprovante_agente_path = (
            comprovante_agente.arquivo_referencia if comprovante_agente else ""
        )
        return pagamento

    @staticmethod
    def _upsert_initial_payment(
        contrato: Contrato,
        *,
        user,
        paid_at: datetime,
    ) -> Pagamento:
        pagamento = get_initial_payment_for_contract(contrato)
        if pagamento is None:
            pagamento = Pagamento(
                cadastro=contrato.associado,
                created_by=user,
                contrato_codigo=contrato.codigo,
                contrato_valor_antecipacao=(
                    contrato.valor_liquido or contrato.valor_total_antecipacao
                ),
                contrato_margem_disponivel=contrato.margem_disponivel,
                cpf_cnpj=contrato.associado.cpf_cnpj,
                full_name=contrato.associado.nome_completo,
                agente_responsavel=contrato.agente.full_name if contrato.agente else "",
                origem=Pagamento.Origem.OPERACIONAL,
                forma_pagamento="pix",
                referencias_externas={
                    "payment_kind": "contrato_inicial",
                    "contrato_id": contrato.id,
                },
                notes="Efetivação inicial do contrato pela tesouraria.",
            )

        pagamento.created_by = pagamento.created_by or user
        pagamento.contrato_valor_antecipacao = (
            contrato.valor_liquido or contrato.valor_total_antecipacao
        )
        pagamento.contrato_margem_disponivel = contrato.margem_disponivel
        pagamento.cpf_cnpj = contrato.associado.cpf_cnpj
        pagamento.full_name = contrato.associado.nome_completo
        pagamento.agente_responsavel = contrato.agente.full_name if contrato.agente else ""
        pagamento.origem = Pagamento.Origem.OPERACIONAL
        pagamento.status = Pagamento.Status.PAGO
        pagamento.valor_pago = contrato.valor_liquido or contrato.valor_total_antecipacao
        pagamento.paid_at = paid_at
        pagamento.forma_pagamento = pagamento.forma_pagamento or "pix"
        pagamento.referencias_externas = {
            **(pagamento.referencias_externas or {}),
            "payment_kind": "contrato_inicial",
            "contrato_id": contrato.id,
        }
        pagamento.notes = "Efetivação inicial do contrato pela tesouraria."
        pagamento = TesourariaService._sync_initial_payment_paths(pagamento, contrato)
        pagamento.save()
        return pagamento

    @staticmethod
    def _sync_contract_evidence_cycle(contrato: Contrato):
        ciclo = contrato.ciclos.order_by("numero").first()
        if ciclo is None:
            return None
        contrato.comprovantes.filter(
            refinanciamento__isnull=True,
            tipo__in=[
                Comprovante.Tipo.COMPROVANTE_PAGAMENTO_ASSOCIADO,
                Comprovante.Tipo.COMPROVANTE_PAGAMENTO_AGENTE,
            ],
        ).update(ciclo=ciclo, updated_at=timezone.now())
        return ciclo

    @staticmethod
    def _require_contract_effectivation_comprovantes(
        contrato: Contrato,
    ) -> tuple[Comprovante, Comprovante]:
        comprovante_associado = TesourariaService._latest_contract_payment_comprovante(
            contrato,
            Comprovante.Papel.ASSOCIADO,
        )
        comprovante_agente = TesourariaService._latest_contract_payment_comprovante(
            contrato,
            Comprovante.Papel.AGENTE,
        )
        errors: dict[str, list[str]] = {}
        if comprovante_associado is None:
            errors["comprovante_associado"] = [
                "O comprovante do associado é obrigatório para efetivar o contrato."
            ]
        if comprovante_agente is None:
            errors["comprovante_agente"] = [
                "O comprovante do agente é obrigatório para efetivar o contrato."
            ]
        if errors:
            raise ValidationError(errors)
        return comprovante_associado, comprovante_agente

    @staticmethod
    def _finalize_contract_effectivation(
        contrato: Contrato,
        *,
        user,
        paid_at: datetime,
        competencias_ciclo=None,
    ) -> Contrato:
        esteira_item = getattr(contrato.associado, "esteira_item", None)
        if not esteira_item or esteira_item.etapa_atual != EsteiraItem.Etapa.TESOURARIA:
            raise ValidationError(
                "Contrato não está disponível para efetivação na tesouraria."
            )

        reactivation_competencias = TesourariaService._normalize_reactivation_competencias(
            contrato,
            competencias_ciclo,
        )

        contrato.status = Contrato.Status.ATIVO
        contrato.auxilio_liberado_em = timezone.localdate()
        if not contrato.data_aprovacao:
            contrato.data_aprovacao = timezone.localdate()
        contrato.save(
            update_fields=[
                "status",
                "auxilio_liberado_em",
                "data_aprovacao",
                "updated_at",
            ]
        )

        contrato.associado.status = Associado.Status.ATIVO
        contrato.associado.save(update_fields=["status", "updated_at"])

        if contrato.origem_operacional == Contrato.OrigemOperacional.REATIVACAO:
            TesourariaService._prepare_reactivation_cycle_effectivation(
                contrato,
                competencias=reactivation_competencias,
            )
            contrato.refresh_from_db()
        else:
            rebuild_contract_cycle_state(contrato, execute=True)
            contrato.refresh_from_db()
        TesourariaService._sync_contract_evidence_cycle(contrato)
        TesourariaService._upsert_initial_payment(contrato, user=user, paid_at=paid_at)

        de_situacao = esteira_item.status
        esteira_item.etapa_atual = EsteiraItem.Etapa.CONCLUIDO
        esteira_item.status = EsteiraItem.Situacao.APROVADO
        esteira_item.tesoureiro_responsavel = user
        esteira_item.concluido_em = paid_at
        esteira_item.save()

        Transicao.objects.create(
            esteira_item=esteira_item,
            acao="efetivar",
            de_status=EsteiraItem.Etapa.TESOURARIA,
            para_status=EsteiraItem.Etapa.CONCLUIDO,
            de_situacao=de_situacao,
            para_situacao=EsteiraItem.Situacao.APROVADO,
            realizado_por=user,
            observacao="Contrato efetivado pela tesouraria.",
        )
        contrato.refresh_from_db()
        return contrato

    @staticmethod
    @transaction.atomic
    def efetivar_contrato(
        contrato_id,
        comprovante_associado,
        comprovante_agente,
        user,
        *,
        competencias_ciclo=None,
    ):
        contrato = TesourariaService._get_contrato(int(contrato_id))
        paid_at = timezone.now()
        if comprovante_associado:
            TesourariaService._upsert_contract_payment_comprovante(
                contrato,
                papel=Comprovante.Papel.ASSOCIADO,
                arquivo=comprovante_associado,
                user=user,
                data_pagamento=paid_at,
            )
        if comprovante_agente:
            TesourariaService._upsert_contract_payment_comprovante(
                contrato,
                papel=Comprovante.Papel.AGENTE,
                arquivo=comprovante_agente,
                user=user,
                data_pagamento=paid_at,
            )
        comprovante_associado_db, comprovante_agente_db = (
            TesourariaService._require_contract_effectivation_comprovantes(contrato)
        )
        for comprovante in (comprovante_associado_db, comprovante_agente_db):
            if comprovante.data_pagamento != paid_at:
                comprovante.data_pagamento = paid_at
                comprovante.save(update_fields=["data_pagamento", "updated_at"])
        return TesourariaService._finalize_contract_effectivation(
            contrato,
            user=user,
            paid_at=paid_at,
            competencias_ciclo=competencias_ciclo,
        )

    @staticmethod
    @transaction.atomic
    def averbar_contrato(contrato_id, user):
        contrato = TesourariaService._get_contrato(int(contrato_id))
        esteira_item = getattr(contrato.associado, "esteira_item", None)
        if not esteira_item or esteira_item.etapa_atual != EsteiraItem.Etapa.TESOURARIA:
            raise ValidationError("Contrato não está disponível para averbação na tesouraria.")
        if not TesourariaService.dispensa_pagamento_inicial(contrato):
            raise ValidationError("A averbação direta só está disponível para contratos sem mensalidade.")

        today = timezone.localdate()
        now = timezone.now()
        contrato.status = Contrato.Status.ATIVO
        contrato.auxilio_liberado_em = today
        if not contrato.data_aprovacao:
            contrato.data_aprovacao = today
        contrato.save(
            update_fields=[
                "status",
                "auxilio_liberado_em",
                "data_aprovacao",
                "updated_at",
            ]
        )

        contrato.associado.status = Associado.Status.ATIVO
        contrato.associado.save(update_fields=["status", "updated_at"])

        de_situacao = esteira_item.status
        esteira_item.etapa_atual = EsteiraItem.Etapa.CONCLUIDO
        esteira_item.status = EsteiraItem.Situacao.APROVADO
        esteira_item.tesoureiro_responsavel = user
        esteira_item.concluido_em = now
        esteira_item.save()

        Transicao.objects.create(
            esteira_item=esteira_item,
            acao="averbar",
            de_status=EsteiraItem.Etapa.TESOURARIA,
            para_status=EsteiraItem.Etapa.CONCLUIDO,
            de_situacao=de_situacao,
            para_situacao=EsteiraItem.Situacao.APROVADO,
            realizado_por=user,
            observacao="Contrato sem mensalidade averbado pela tesouraria.",
        )
        return contrato

    @staticmethod
    @transaction.atomic
    def congelar_contrato(contrato_id, motivo, user):
        contrato = TesourariaService._get_contrato(int(contrato_id))
        esteira_item = getattr(contrato.associado, "esteira_item", None)
        if not esteira_item or esteira_item.etapa_atual != EsteiraItem.Etapa.TESOURARIA:
            raise ValidationError("Contrato não está na etapa de tesouraria.")

        de_situacao = esteira_item.status
        esteira_item.status = EsteiraItem.Situacao.PENDENCIADO
        esteira_item.tesoureiro_responsavel = user
        esteira_item.observacao = motivo
        esteira_item.save()

        Transicao.objects.create(
            esteira_item=esteira_item,
            acao="congelar",
            de_status=EsteiraItem.Etapa.TESOURARIA,
            para_status=EsteiraItem.Etapa.TESOURARIA,
            de_situacao=de_situacao,
            para_situacao=EsteiraItem.Situacao.PENDENCIADO,
            realizado_por=user,
            observacao=motivo,
        )
        return contrato

    @staticmethod
    @transaction.atomic
    def pendenciar_para_analise(
        contrato_id: int,
        *,
        tipo: str,
        descricao: str,
        user,
    ) -> Contrato:
        contrato = TesourariaService._get_contrato(int(contrato_id))
        esteira_item = getattr(contrato.associado, "esteira_item", None)
        if not esteira_item or esteira_item.etapa_atual != EsteiraItem.Etapa.TESOURARIA:
            raise ValidationError(
                "Contrato não está disponível para retorno da tesouraria para a análise."
            )

        descricao_normalizada = (descricao or "").strip()
        if not descricao_normalizada:
            raise ValidationError({"descricao": ["Descreva o motivo da pendência."]})

        from apps.esteira.services import EsteiraService

        EsteiraService.pendenciar(
            esteira_item,
            user,
            (tipo or "tesouraria").strip(),
            descricao_normalizada,
        )
        contrato.refresh_from_db()
        return contrato

    @staticmethod
    def obter_dados_bancarios(contrato_id):
        contrato = TesourariaService._get_contrato(int(contrato_id))
        return contrato.associado.build_dados_bancarios_payload()

    @staticmethod
    @transaction.atomic
    def substituir_comprovante(contrato_id, *, papel: str, arquivo, user):
        contrato = TesourariaService._get_contrato(int(contrato_id))
        now = timezone.now()
        TesourariaService._upsert_contract_payment_comprovante(
            contrato,
            papel=papel,
            arquivo=arquivo,
            user=user,
            data_pagamento=now,
        )
        pagamento = get_initial_payment_for_contract(contrato)
        if pagamento is not None:
            TesourariaService._sync_initial_payment_paths(pagamento, contrato)
            pagamento.save(
                update_fields=[
                    "comprovante_associado_path",
                    "comprovante_agente_path",
                    "updated_at",
                ]
            )
        contrato.refresh_from_db()
        return contrato

    @staticmethod
    @transaction.atomic
    def excluir_contrato_operacional(contrato_id: int, user) -> Contrato:
        contrato = TesourariaService._get_contrato(int(contrato_id))
        esteira_item = getattr(contrato.associado, "esteira_item", None)
        if esteira_item is None:
            raise ValidationError("Contrato sem item operacional vinculado.")
        from apps.esteira.services import EsteiraService

        observacao = "Linha operacional removida pela tesouraria com histórico preservado."

        # Contrato ja cancelado/encerrado: nao fechar a esteira caso ainda exista
        # uma reativacao pendente ativa (evita cancelar o contrato mais recente).
        if contrato.status in [Contrato.Status.CANCELADO, Contrato.Status.ENCERRADO]:
            pending = EsteiraService._pending_reactivation_contract(esteira_item)
            if pending is not None:
                # Oculta o contrato antigo da fila sem tocar na esteira nem no pendente.
                soft_delete_contract_tree(contrato)
                TesourariaService._registrar_remocao_contrato_sem_fechar_esteira(
                    esteira_item,
                    user=user,
                    observacao=observacao,
                )
                return contrato
            # Sem reativacao pendente: fecha a esteira normalmente
            soft_delete_contract_tree(contrato)
            # Esteira já concluída: apenas oculta o item sem alterar seu estado.
            if esteira_item.etapa_atual == EsteiraItem.Etapa.CONCLUIDO:
                esteira_item.soft_delete()
                return contrato
            EsteiraService.remover_fila_operacional(
                esteira_item,
                user,
                observacao=observacao,
            )
            return contrato

        if TesourariaService._is_pending_reactivation_contract(contrato):
            now = timezone.now()
            contrato.status = Contrato.Status.CANCELADO
            contrato.cancelamento_tipo = Contrato.CancelamentoTipo.CANCELADO
            contrato.cancelamento_motivo = observacao
            contrato.cancelado_em = now
            contrato.save(
                update_fields=[
                    "status",
                    "cancelamento_tipo",
                    "cancelamento_motivo",
                    "cancelado_em",
                    "updated_at",
                ]
            )
            soft_delete_contract_tree(contrato)

            if TesourariaService._has_remaining_open_operational_contract(contrato):
                TesourariaService._registrar_remocao_contrato_sem_fechar_esteira(
                    esteira_item,
                    user=user,
                    observacao=observacao,
                )
                return contrato

            EsteiraService.remover_fila_operacional(
                esteira_item,
                user,
                observacao=observacao,
            )
            Associado.objects.filter(pk=contrato.associado_id).update(
                status=Associado.Status.INATIVO,
                updated_at=now,
            )
            return contrato

        # Esteira já concluída (ex.: reativação já efetivada e aprovada):
        # apenas oculta o item da fila via soft-delete sem alterar seu estado.
        if esteira_item.etapa_atual == EsteiraItem.Etapa.CONCLUIDO:
            esteira_item.soft_delete()
            contrato.refresh_from_db()
            return contrato

        EsteiraService.remover_fila_operacional(
            esteira_item,
            user,
            observacao=observacao,
        )
        contrato.refresh_from_db()
        return contrato

    @staticmethod
    @transaction.atomic
    def cancelar_contrato(contrato_id: int, tipo: str, motivo: str, user) -> Contrato:
        contrato = TesourariaService._get_contrato(int(contrato_id))
        if contrato.status == Contrato.Status.ATIVO:
            raise ValidationError("Somente contratos ainda não ativos podem ser cancelados.")
        if contrato.status == Contrato.Status.ENCERRADO:
            raise ValidationError("Contratos liquidados não podem ser cancelados.")
        if contrato.status == Contrato.Status.CANCELADO:
            raise ValidationError("Este contrato já foi cancelado.")

        now = timezone.now()
        contrato.status = Contrato.Status.CANCELADO
        contrato.cancelamento_tipo = tipo
        contrato.cancelamento_motivo = motivo.strip()
        contrato.cancelado_em = now
        contrato.save(
            update_fields=[
                "status",
                "cancelamento_tipo",
                "cancelamento_motivo",
                "cancelado_em",
                "updated_at",
            ]
        )

        esteira_item = getattr(contrato.associado, "esteira_item", None)
        if esteira_item:
            de_etapa = esteira_item.etapa_atual
            de_situacao = esteira_item.status
            esteira_item.etapa_atual = EsteiraItem.Etapa.CONCLUIDO
            esteira_item.status = EsteiraItem.Situacao.REJEITADO
            esteira_item.observacao = contrato.cancelamento_motivo
            esteira_item.tesoureiro_responsavel = user
            esteira_item.concluido_em = now
            esteira_item.save(
                update_fields=[
                    "etapa_atual",
                    "status",
                    "observacao",
                    "tesoureiro_responsavel",
                    "concluido_em",
                    "updated_at",
                ]
            )
            Transicao.objects.create(
                esteira_item=esteira_item,
                acao="cancelar_contrato",
                de_status=de_etapa,
                para_status=EsteiraItem.Etapa.CONCLUIDO,
                de_situacao=de_situacao,
                para_situacao=EsteiraItem.Situacao.REJEITADO,
                realizado_por=user,
                observacao=contrato.cancelamento_motivo,
            )

        TesourariaService._restore_associado_status_after_pending_reativacao(contrato)
        if contrato.origem_operacional == Contrato.OrigemOperacional.REATIVACAO:
            Associado.objects.filter(pk=contrato.associado_id).update(
                status=Associado.Status.INATIVO,
                updated_at=timezone.now(),
            )
        return contrato


class ConfirmacaoService:
    @staticmethod
    def _garantir_registros(contrato: Contrato, competencia: date) -> tuple[Confirmacao, Confirmacao]:
        ligacao, _ = Confirmacao.objects.get_or_create(
            contrato=contrato,
            competencia=competencia,
            tipo=Confirmacao.Tipo.LIGACAO,
        )
        averbacao, _ = Confirmacao.objects.get_or_create(
            contrato=contrato,
            competencia=competencia,
            tipo=Confirmacao.Tipo.AVERBACAO,
        )
        return ligacao, averbacao

    @staticmethod
    def _serialize_pair(ligacao: Confirmacao, averbacao: Confirmacao) -> dict[str, object]:
        contrato = ligacao.contrato
        return {
            "id": ligacao.id,
            "contrato_id": contrato.id,
            "associado_id": contrato.associado_id,
            "nome": contrato.associado.nome_completo,
            "cpf_cnpj": contrato.associado.cpf_cnpj,
            "agente_nome": contrato.agente.full_name if contrato.agente else "",
            "competencia": ligacao.competencia,
            "link_chamada": ligacao.link_chamada,
            "ligacao_confirmada": ligacao.status == Confirmacao.Status.CONFIRMADO,
            "averbacao_confirmada": averbacao.status == Confirmacao.Status.CONFIRMADO,
            "status_visual": (
                "averbacao_confirmada"
                if averbacao.status == Confirmacao.Status.CONFIRMADO
                else "ligacao_recebida"
                if ligacao.status == Confirmacao.Status.CONFIRMADO
                else "sem_link"
            ),
        }

    @staticmethod
    def _get_ligacao(confirmacao_id: int) -> Confirmacao:
        try:
            confirmacao = Confirmacao.objects.select_related(
                "contrato",
                "contrato__associado",
                "contrato__agente",
            ).get(pk=confirmacao_id, tipo=Confirmacao.Tipo.LIGACAO)
        except Confirmacao.DoesNotExist as exc:
            raise ValidationError("Confirmação de ligação não encontrada.") from exc
        return confirmacao

    @staticmethod
    def listar_por_competencia(competencia: date) -> list[dict[str, object]]:
        contratos = (
            Contrato.objects.select_related("associado", "agente")
            .filter(status__in=[Contrato.Status.ATIVO, Contrato.Status.ENCERRADO])
            .order_by("associado__nome_completo")
        )

        payload: list[dict[str, object]] = []
        for contrato in contratos:
            ligacao, averbacao = ConfirmacaoService._garantir_registros(
                contrato, competencia
            )
            payload.append(ConfirmacaoService._serialize_pair(ligacao, averbacao))
        return payload

    @staticmethod
    def obter(confirmacao_id: int) -> dict[str, object]:
        ligacao = ConfirmacaoService._get_ligacao(confirmacao_id)
        _, averbacao = ConfirmacaoService._garantir_registros(
            ligacao.contrato, ligacao.competencia
        )
        return ConfirmacaoService._serialize_pair(ligacao, averbacao)

    @staticmethod
    @transaction.atomic
    def salvar_link_chamada(confirmacao_id, link):
        ligacao = ConfirmacaoService._get_ligacao(int(confirmacao_id))
        ligacao.link_chamada = link
        ligacao.save(update_fields=["link_chamada", "updated_at"])
        return ConfirmacaoService.obter(ligacao.id)

    @staticmethod
    @transaction.atomic
    def confirmar_ligacao(confirmacao_id, user):
        ligacao = ConfirmacaoService._get_ligacao(int(confirmacao_id))
        if not ligacao.link_chamada:
            raise ValidationError("Salve o link da chamada antes de confirmar a ligação.")
        ligacao.confirmar(user)
        return ConfirmacaoService.obter(ligacao.id)

    @staticmethod
    @transaction.atomic
    def confirmar_averbacao(confirmacao_id, user):
        ligacao = ConfirmacaoService._get_ligacao(int(confirmacao_id))
        if ligacao.status != Confirmacao.Status.CONFIRMADO:
            raise ValidationError("Confirme a ligação antes de confirmar a averbação.")
        _, averbacao = ConfirmacaoService._garantir_registros(
            ligacao.contrato, ligacao.competencia
        )
        averbacao.confirmar(user)
        return ConfirmacaoService.obter(ligacao.id)


class BaixaManualService:
    @staticmethod
    def listar_associados_para_inadimplencia(search: str | None = None) -> list[dict[str, object]]:
        normalized = str(search or "").strip()
        queryset = (
            Associado.objects.filter(
                deleted_at__isnull=True,
                contratos__deleted_at__isnull=True,
                contratos__contrato_canonico__isnull=True,
            )
            .select_related("agente_responsavel")
            .distinct()
        )
        if normalized:
            queryset = queryset.filter(
                Q(nome_completo__icontains=normalized)
                | Q(cpf_cnpj__icontains=normalized)
                | Q(matricula__icontains=normalized)
                | Q(matricula_orgao__icontains=normalized)
            )
        queryset = queryset.order_by("nome_completo", "id")[:20]
        return [
            {
                "id": associado.id,
                "nome": associado.nome_completo,
                "cpf_cnpj": associado.cpf_cnpj,
                "matricula": associado.matricula_orgao or associado.matricula or "",
                "status": associado.status,
                "agente_nome": (
                    associado.agente_responsavel.full_name
                    if associado.agente_responsavel_id and associado.agente_responsavel
                    else ""
                ),
            }
            for associado in queryset
        ]

    @staticmethod
    def _resolve_contract_for_manual_inadimplencia(associado: Associado) -> Contrato:
        contratos = get_operational_contracts_for_associado(associado)
        if contratos:
            return max(contratos, key=lambda contrato: (contrato.created_at, contrato.id))

        if associado.status == Associado.Status.INATIVO:
            contrato = (
                Contrato.objects.filter(
                    associado=associado,
                    deleted_at__isnull=True,
                    contrato_canonico__isnull=True,
                )
                .select_related("agente")
                .order_by("-created_at", "-id")
                .first()
            )
            if contrato is not None:
                return contrato

        raise ValidationError(
            "O associado não possui contrato operacional visível para registrar inadimplência."
        )

    @staticmethod
    def _resolve_manual_inadimplencia_cycle(
        contrato: Contrato,
        *,
        referencia_mes: date,
        valor: Decimal,
    ) -> Ciclo:
        ciclo = (
            Ciclo.all_objects.filter(contrato=contrato, deleted_at__isnull=True)
            .order_by("-numero", "-id")
            .first()
        )
        if ciclo is not None:
            return ciclo

        next_number = (
            Ciclo.all_objects.filter(contrato=contrato).aggregate(max_numero=Max("numero"))[
                "max_numero"
            ]
            or 0
        ) + 1
        return Ciclo.objects.create(
            contrato=contrato,
            numero=next_number,
            data_inicio=referencia_mes,
            data_fim=referencia_mes,
            status=Ciclo.Status.PENDENCIA,
            valor_total=valor,
        )

    @staticmethod
    def _next_parcela_number(ciclo: Ciclo) -> int:
        return (
            Parcela.all_objects.filter(ciclo=ciclo).aggregate(max_numero=Max("numero"))[
                "max_numero"
            ]
            or 0
        ) + 1

    @staticmethod
    def _default_due_date(referencia_mes: date) -> date:
        return referencia_mes.replace(
            day=min(5, monthrange(referencia_mes.year, referencia_mes.month)[1])
        )

    @staticmethod
    def _manual_pending_source(parcela: Parcela) -> str:
        if parcela.layout_bucket == Parcela.LayoutBucket.UNPAID:
            return "manual"
        return "parcela"

    @staticmethod
    def _can_remove_manual_inadimplencia(parcela: Parcela) -> bool:
        return (
            parcela.layout_bucket == Parcela.LayoutBucket.UNPAID
            and parcela.status in {Parcela.Status.EM_ABERTO, Parcela.Status.NAO_DESCONTADO}
            and not parcela_has_financial_evidence(parcela)
            and not parcela.itens_retorno.exists()
        )

    @staticmethod
    def _apply_parcela_search(queryset, search: str | None):
        if not search:
            return queryset
        return queryset.filter(
            Q(ciclo__contrato__associado__nome_completo__icontains=search)
            | Q(ciclo__contrato__associado__cpf_cnpj__icontains=search)
            | Q(ciclo__contrato__associado__matricula__icontains=search)
            | Q(ciclo__contrato__associado__matricula_orgao__icontains=search)
            | Q(ciclo__contrato__codigo__icontains=search)
        )

    @staticmethod
    def _apply_baixa_search(queryset, search: str | None):
        if not search:
            return queryset
        return queryset.filter(
            Q(parcela__ciclo__contrato__associado__nome_completo__icontains=search)
            | Q(parcela__ciclo__contrato__associado__cpf_cnpj__icontains=search)
            | Q(parcela__ciclo__contrato__associado__matricula__icontains=search)
            | Q(
                parcela__ciclo__contrato__associado__matricula_orgao__icontains=search
            )
            | Q(parcela__ciclo__contrato__codigo__icontains=search)
        )

    @staticmethod
    def _apply_parcela_agent_filter(queryset, agente: str | None):
        agente_term = (agente or "").strip()
        if not agente_term:
            return queryset
        if agente_term.isdigit():
            agente_id = int(agente_term)
            return queryset.filter(
                Q(ciclo__contrato__agente_id=agente_id)
                | Q(ciclo__contrato__associado__agente_responsavel_id=agente_id)
            )
        return queryset.filter(
            Q(ciclo__contrato__agente__first_name__icontains=agente_term)
            | Q(ciclo__contrato__agente__last_name__icontains=agente_term)
            | Q(ciclo__contrato__agente__email__icontains=agente_term)
            | Q(
                ciclo__contrato__associado__agente_responsavel__first_name__icontains=agente_term
            )
            | Q(
                ciclo__contrato__associado__agente_responsavel__last_name__icontains=agente_term
            )
            | Q(
                ciclo__contrato__associado__agente_responsavel__email__icontains=agente_term
            )
        )

    @staticmethod
    def _apply_baixa_agent_filter(queryset, agente: str | None):
        agente_term = (agente or "").strip()
        if not agente_term:
            return queryset
        if agente_term.isdigit():
            agente_id = int(agente_term)
            return queryset.filter(
                Q(parcela__ciclo__contrato__agente_id=agente_id)
                | Q(
                    parcela__ciclo__contrato__associado__agente_responsavel_id=agente_id
                )
            )
        return queryset.filter(
            Q(parcela__ciclo__contrato__agente__first_name__icontains=agente_term)
            | Q(parcela__ciclo__contrato__agente__last_name__icontains=agente_term)
            | Q(parcela__ciclo__contrato__agente__email__icontains=agente_term)
            | Q(
                parcela__ciclo__contrato__associado__agente_responsavel__first_name__icontains=agente_term
            )
            | Q(
                parcela__ciclo__contrato__associado__agente_responsavel__last_name__icontains=agente_term
            )
            | Q(
                parcela__ciclo__contrato__associado__agente_responsavel__email__icontains=agente_term
            )
        )

    @staticmethod
    def _parse_item_competencia(value: str | None) -> date | None:
        if not value:
            return None

        normalized = value.strip()
        for pattern in ("%m/%Y", "%Y-%m"):
            try:
                return datetime.strptime(normalized, pattern).date().replace(day=1)
            except ValueError:
                continue
        return None

    @classmethod
    def _latest_return_file_ids(
        cls,
        *,
        competencia: date | None = None,
    ) -> list[int]:
        base_queryset = ArquivoRetorno.objects.filter(status="concluido")
        if competencia:
            latest = (
                base_queryset.filter(competencia=competencia)
                .order_by("-processado_em", "-created_at", "-id")
                .first()
            )
            return [latest.id] if latest else []

        file_ids_by_month: dict[date, int] = {}
        for arquivo in base_queryset.order_by("-competencia", "-processado_em", "-created_at", "-id"):
            if arquivo.competencia not in file_ids_by_month:
                file_ids_by_month[arquivo.competencia] = arquivo.id
        return list(file_ids_by_month.values())

    @staticmethod
    def _build_pending_row_from_parcela(
        parcela: Parcela,
        *,
        source: str | None = None,
        arquivo_retorno_item_id: int | None = None,
    ) -> dict[str, object]:
        contrato = parcela.ciclo.contrato
        associado = contrato.associado
        agente = contrato.agente or associado.agente_responsavel
        resolved_source = source or BaixaManualService._manual_pending_source(parcela)
        return {
            "id": parcela.id,
            "parcela_id": parcela.id,
            "associado_id": associado.id,
            "nome": associado.nome_completo,
            "cpf_cnpj": associado.cpf_cnpj,
            "matricula": associado.matricula_orgao or associado.matricula or "",
            "agente_nome": agente.full_name if agente else "",
            "contrato_id": contrato.id,
            "contrato_codigo": contrato.codigo,
            "referencia_mes": parcela.referencia_mes,
            "valor": parcela.valor,
            "status": parcela.status,
            "data_vencimento": parcela.data_vencimento,
            "observacao": parcela.observacao,
            "data_baixa": None,
            "valor_pago": None,
            "realizado_por_nome": "",
            "nome_comprovante": "",
            "origem": resolved_source,
            "arquivo_retorno_item_id": arquivo_retorno_item_id,
            "pode_dar_baixa": True,
            "_agente_id": agente.id if agente else None,
            "_sort_nome": associado.nome_completo or "",
            "_sort_referencia": parcela.referencia_mes,
        }

    @classmethod
    def _build_pending_row_from_return_item(
        cls,
        item: ArquivoRetornoItem,
    ) -> dict[str, object] | None:
        referencia = cls._parse_item_competencia(item.competencia)
        if referencia is None:
            return None

        parcela = item.parcela
        if parcela and parcela.status not in {
            Parcela.Status.EM_ABERTO,
            Parcela.Status.NAO_DESCONTADO,
        }:
            return None

        associado = item.associado or getattr(parcela, "associado", None)
        if associado is None:
            return None

        contrato = getattr(getattr(parcela, "ciclo", None), "contrato", None)
        agente = None
        if contrato and contrato.agente:
            agente = contrato.agente
        elif associado.agente_responsavel:
            agente = associado.agente_responsavel

        return {
            "id": -item.id,
            "parcela_id": item.parcela_id,
            "associado_id": associado.id,
            "nome": associado.nome_completo or item.nome_servidor,
            "cpf_cnpj": associado.cpf_cnpj or item.cpf_cnpj,
            "matricula": associado.matricula_orgao or associado.matricula or item.matricula_servidor,
            "agente_nome": agente.full_name if agente else "",
            "contrato_id": contrato.id if contrato else None,
            "contrato_codigo": contrato.codigo if contrato else "",
            "referencia_mes": referencia,
            "valor": item.valor_descontado,
            "status": Parcela.Status.NAO_DESCONTADO,
            "data_vencimento": getattr(parcela, "data_vencimento", None),
            "observacao": item.observacao or item.motivo_rejeicao or "",
            "data_baixa": None,
            "valor_pago": None,
            "realizado_por_nome": "",
            "nome_comprovante": "",
            "origem": "arquivo_retorno",
            "arquivo_retorno_item_id": item.id,
            "pode_dar_baixa": True,
            "_agente_id": agente.id if agente else None,
            "_sort_nome": associado.nome_completo or item.nome_servidor or "",
            "_sort_referencia": referencia,
        }

    @staticmethod
    def _build_quitado_row_from_manual_payment(
        pagamento: PagamentoMensalidade,
    ) -> dict[str, object] | None:
        associado = pagamento.associado
        if associado is None:
            return None

        agente = associado.agente_responsavel
        data_baixa = pagamento.manual_paid_at.date() if pagamento.manual_paid_at else None
        valor_pago = pagamento.recebido_manual or pagamento.valor
        if data_baixa is None or valor_pago is None:
            return None

        return {
            "id": -pagamento.id,
            "parcela_id": None,
            "associado_id": associado.id,
            "nome": associado.nome_completo,
            "cpf_cnpj": associado.cpf_cnpj,
            "matricula": associado.matricula_orgao or associado.matricula or pagamento.matricula,
            "agente_nome": agente.full_name if agente else "",
            "contrato_id": None,
            "contrato_codigo": "",
            "referencia_mes": pagamento.referencia_month,
            "valor": valor_pago,
            "status": Parcela.Status.DESCONTADO,
            "data_vencimento": None,
            "observacao": "",
            "data_baixa": data_baixa,
            "valor_pago": valor_pago,
            "realizado_por_nome": pagamento.manual_by.full_name if pagamento.manual_by else "",
            "nome_comprovante": basename(pagamento.manual_comprovante_path or ""),
            "origem": "pagamento_manual",
            "arquivo_retorno_item_id": None,
            "pode_dar_baixa": False,
            "_agente_id": agente.id if agente else None,
            "_sort_nome": associado.nome_completo or "",
            "_sort_referencia": pagamento.referencia_month,
        }

    @staticmethod
    def _manual_payment_has_related_parcela(pagamento: PagamentoMensalidade) -> bool:
        if pagamento.associado_id is None:
            return False
        return Parcela.objects.filter(
            associado_id=pagamento.associado_id,
            referencia_mes=pagamento.referencia_month,
            deleted_at__isnull=True,
        ).exclude(status=Parcela.Status.CANCELADO).exists()

    @staticmethod
    def _store_manual_payment_proof(comprovante) -> str:
        original_name = getattr(comprovante, "name", "") or "comprovante.pdf"
        safe_name = slugify(original_name.rsplit(".", 1)[0]) or "comprovante"
        extension = f".{original_name.rsplit('.', 1)[1]}" if "." in original_name else ""
        content = comprovante.read()
        if not content:
            raise ValidationError("Envie um comprovante válido para registrar a baixa.")
        file_name = f"baixas_manuais/manual-{timezone.now().strftime('%Y%m%d%H%M%S')}-{uuid4().hex[:8]}-{safe_name}{extension}"
        return default_storage.save(file_name, ContentFile(content))

    @classmethod
    def _register_manual_payment_without_parcela(
        cls,
        item: ArquivoRetornoItem,
        *,
        comprovante,
        valor_pago,
        observacao: str,
        user,
    ) -> PagamentoMensalidade:
        associado = item.associado
        if associado is None:
            raise ValidationError("O item do retorno não possui associado vinculado para quitação.")

        referencia = cls._parse_item_competencia(item.competencia)
        if referencia is None:
            raise ValidationError("Não foi possível identificar a competência do item do retorno.")

        payment_identity = build_payment_identity(
            cpf_cnpj=associado.cpf_cnpj or item.cpf_cnpj,
            referencia_month=referencia,
            matricula=associado.matricula_orgao or associado.matricula or item.matricula_servidor or "",
        )
        pagamentos = list(
            PagamentoMensalidade.objects.filter(
                cpf_cnpj=payment_identity[0],
                referencia_month=payment_identity[1],
            ).select_related("associado", "manual_by")
        )
        pagamento = next(
            (
                candidate
                for candidate in pagamentos
                if build_payment_identity(
                    cpf_cnpj=candidate.cpf_cnpj,
                    referencia_month=candidate.referencia_month,
                    matricula=candidate.matricula or "",
                )
                == payment_identity
            ),
            None,
        )

        comprovante_path = cls._store_manual_payment_proof(comprovante)
        manual_paid_at = timezone.make_aware(datetime.combine(timezone.localdate(), time(12, 0)))
        default_valor = Decimal(str(item.valor_descontado or valor_pago or "0"))

        if pagamento is None:
            pagamento = PagamentoMensalidade.objects.create(
                created_by=user,
                import_uuid=f"manual-{associado.id}-{referencia.strftime('%Y%m')}",
                referencia_month=referencia,
                status_code="M",
                matricula=associado.matricula_orgao or associado.matricula or item.matricula_servidor or "",
                orgao_pagto=item.orgao_pagto_nome or "",
                nome_relatorio=associado.nome_completo or item.nome_servidor or "",
                cpf_cnpj=associado.cpf_cnpj or item.cpf_cnpj,
                associado=associado,
                valor=default_valor,
                recebido_manual=valor_pago,
                manual_status=PagamentoMensalidade.ManualStatus.PAGO,
                manual_paid_at=manual_paid_at,
                manual_forma_pagamento="baixa_manual",
                manual_comprovante_path=comprovante_path,
                manual_by=user,
                source_file_path=getattr(item.arquivo_retorno, "arquivo_url", "") or "manual/baixa-manual",
            )
        else:
            pagamento.associado = associado
            pagamento.status_code = "M"
            pagamento.matricula = associado.matricula_orgao or associado.matricula or item.matricula_servidor or ""
            pagamento.orgao_pagto = item.orgao_pagto_nome or pagamento.orgao_pagto or ""
            pagamento.nome_relatorio = associado.nome_completo or item.nome_servidor or pagamento.nome_relatorio
            pagamento.valor = pagamento.valor or default_valor
            pagamento.recebido_manual = valor_pago
            pagamento.manual_status = PagamentoMensalidade.ManualStatus.PAGO
            pagamento.manual_paid_at = manual_paid_at
            pagamento.manual_forma_pagamento = "baixa_manual"
            pagamento.manual_comprovante_path = comprovante_path
            pagamento.manual_by = user
            if not pagamento.source_file_path:
                pagamento.source_file_path = getattr(item.arquivo_retorno, "arquivo_url", "") or "manual/baixa-manual"
            pagamento.save(
                update_fields=[
                    "associado",
                    "status_code",
                    "matricula",
                    "orgao_pagto",
                    "nome_relatorio",
                    "valor",
                    "recebido_manual",
                    "manual_status",
                    "manual_paid_at",
                    "manual_forma_pagamento",
                    "manual_comprovante_path",
                    "manual_by",
                    "source_file_path",
                    "updated_at",
                ]
            )

        item.resultado_processamento = ArquivoRetornoItem.ResultadoProcessamento.BAIXA_EFETUADA
        item.observacao = observacao or item.observacao or "Baixa manual registrada sem parcela vinculada."
        item.save(update_fields=["resultado_processamento", "observacao", "updated_at"])
        return pagamento

    @staticmethod
    def _pending_row_matches_search(row: dict[str, object], search: str | None) -> bool:
        search_term = (search or "").strip().lower()
        if not search_term:
            return True

        comparable = " ".join(
            [
                str(row.get("nome") or ""),
                str(row.get("cpf_cnpj") or ""),
                str(row.get("matricula") or ""),
                str(row.get("contrato_codigo") or ""),
            ]
        ).lower()
        return search_term in comparable

    @staticmethod
    def _pending_row_matches_agent(row: dict[str, object], agente: str | None) -> bool:
        agente_term = (agente or "").strip()
        if not agente_term:
            return True
        if agente_term.isdigit():
            return row.get("_agente_id") == int(agente_term)
        return agente_term.lower() in str(row.get("agente_nome") or "").lower()

    @staticmethod
    def _pending_row_matches_dates(
        row: dict[str, object],
        *,
        data_inicio: date | None = None,
        data_fim: date | None = None,
    ) -> bool:
        target_date = row.get("data_vencimento") or row.get("referencia_mes")
        if not isinstance(target_date, date):
            return False
        if data_inicio and target_date < data_inicio:
            return False
        if data_fim and target_date > data_fim:
            return False
        return True

    @classmethod
    def listar_parcelas_pendentes(
        cls,
        search: str | None = None,
        status_filter: str | None = None,
        competencia: date | None = None,
        data_inicio: date | None = None,
        data_fim: date | None = None,
        agente: str | None = None,
    ):
        hoje = timezone.localdate()
        mes_atual = hoje.replace(day=1)
        effective_status = (
            status_filter
            if status_filter in {Parcela.Status.EM_ABERTO, Parcela.Status.NAO_DESCONTADO}
            else None
        )

        rows: list[dict[str, object]] = []
        retorno_parcela_ids: set[int] = set()
        retorno_associado_competencias: set[tuple[int, date]] = set()
        latest_file_ids: list[int] = []

        if effective_status in {None, Parcela.Status.NAO_DESCONTADO}:
            latest_file_ids = cls._latest_return_file_ids(competencia=competencia)
            if latest_file_ids:
                return_items = (
                    ArquivoRetornoItem.objects.select_related(
                        "associado__agente_responsavel",
                        "parcela__associado__agente_responsavel",
                        "parcela__ciclo__contrato__agente",
                    )
                    .filter(
                        arquivo_retorno_id__in=latest_file_ids,
                        resultado_processamento=ArquivoRetornoItem.ResultadoProcessamento.NAO_DESCONTADO,
                    )
                    .order_by("associado__nome_completo", "linha_numero", "id")
                )

                for item in return_items:
                    row = cls._build_pending_row_from_return_item(item)
                    if row is None:
                        continue
                    referencia = row.get("referencia_mes")
                    if not isinstance(referencia, date) or referencia >= mes_atual:
                        continue
                    if not cls._pending_row_matches_dates(
                        row,
                        data_inicio=data_inicio,
                        data_fim=data_fim,
                    ):
                        continue
                    if not cls._pending_row_matches_agent(row, agente):
                        continue
                    if not cls._pending_row_matches_search(row, search):
                        continue

                    parcela_id = row.get("parcela_id")
                    associado_id = row.get("associado_id")
                    if isinstance(parcela_id, int):
                        retorno_parcela_ids.add(parcela_id)
                    if isinstance(associado_id, int):
                        retorno_associado_competencias.add((associado_id, referencia))
                    rows.append(row)

        parcela_queryset = (
            Parcela.objects.select_related(
                "ciclo__contrato__associado__agente_responsavel",
                "ciclo__contrato__agente",
            )
            .filter(
                ciclo__contrato__contrato_canonico__isnull=True,
                status__in=[Parcela.Status.EM_ABERTO, Parcela.Status.NAO_DESCONTADO],
                referencia_mes__lt=mes_atual,
                descartado_em__isnull=True,
            )
            .order_by(
                "ciclo__contrato__associado__nome_completo",
                "referencia_mes",
                "id",
            )
        )

        if competencia:
            parcela_queryset = parcela_queryset.filter(
                referencia_mes__year=competencia.year,
                referencia_mes__month=competencia.month,
            )

        if effective_status in {Parcela.Status.EM_ABERTO, Parcela.Status.NAO_DESCONTADO}:
            parcela_queryset = parcela_queryset.filter(status=effective_status)

        if data_inicio:
            parcela_queryset = parcela_queryset.filter(data_vencimento__gte=data_inicio)

        if data_fim:
            parcela_queryset = parcela_queryset.filter(data_vencimento__lte=data_fim)

        parcela_queryset = cls._apply_parcela_agent_filter(parcela_queryset, agente)
        parcela_queryset = cls._apply_parcela_search(parcela_queryset, search)

        for parcela in parcela_queryset:
            if parcela.status == Parcela.Status.NAO_DESCONTADO:
                if (
                    parcela.id in retorno_parcela_ids
                    or (parcela.associado_id, parcela.referencia_mes)
                    in retorno_associado_competencias
                ):
                    continue
            rows.append(cls._build_pending_row_from_parcela(parcela))

        rows.sort(
            key=lambda row: (
                str(row.get("_sort_nome") or ""),
                row.get("_sort_referencia") or date.min,
                abs(int(row.get("id") or 0)),
            )
        )
        return rows

    @staticmethod
    @transaction.atomic
    def registrar_inadimplencia(
        *,
        associado_id: int,
        referencia_mes: date,
        valor: Decimal,
        status: str,
        observacao: str,
        user,
        data_vencimento: date | None = None,
        quitar_direto: bool = False,
        comprovante=None,
        valor_pago: Decimal | None = None,
    ) -> dict[str, object]:
        try:
            associado = Associado.objects.select_related("agente_responsavel").get(pk=associado_id)
        except Associado.DoesNotExist as exc:
            raise ValidationError("Associado não encontrado.") from exc

        referencia = referencia_mes.replace(day=1)
        mes_atual = timezone.localdate().replace(day=1)
        if referencia >= mes_atual:
            raise ValidationError(
                "A competência da inadimplência precisa ser anterior ao mês atual."
            )

        if status not in {Parcela.Status.EM_ABERTO, Parcela.Status.NAO_DESCONTADO}:
            raise ValidationError("Escolha um status pendente válido para a inadimplência.")

        existing = (
            Parcela.objects.filter(
                associado=associado,
                referencia_mes=referencia,
                descartado_em__isnull=True,
            )
            .exclude(status__in=[Parcela.Status.CANCELADO, Parcela.Status.DESCONTADO, Parcela.Status.LIQUIDADA])
            .order_by("-id")
            .first()
        )
        if existing is not None:
            raise ValidationError(
                "Já existe uma inadimplência pendente para esta competência do associado."
            )

        contrato = BaixaManualService._resolve_contract_for_manual_inadimplencia(associado)
        ciclo = BaixaManualService._resolve_manual_inadimplencia_cycle(
            contrato,
            referencia_mes=referencia,
            valor=valor,
        )
        parcela = Parcela.objects.create(
            ciclo=ciclo,
            associado=associado,
            numero=BaixaManualService._next_parcela_number(ciclo),
            referencia_mes=referencia,
            valor=valor,
            data_vencimento=data_vencimento or BaixaManualService._default_due_date(referencia),
            status=status,
            layout_bucket=Parcela.LayoutBucket.UNPAID,
            observacao=(observacao or "").strip(),
        )

        if not quitar_direto:
            return {
                "id": parcela.id,
                "parcela_id": parcela.id,
                "quitada": False,
                "message": "Inadimplência registrada na fila de pendentes.",
            }

        if comprovante is None:
            raise ValidationError("Envie um comprovante para quitar a inadimplência agora.")

        baixa = BaixaManualService._registrar_baixa_manual(
            parcela,
            comprovante=comprovante,
            valor_pago=valor_pago or valor,
            observacao=(observacao or "").strip(),
            user=user,
        )
        return {
            "id": baixa.id,
            "parcela_id": parcela.id,
            "quitada": True,
            "message": "Inadimplência registrada e quitada com sucesso.",
        }

    @staticmethod
    def descartar_parcela(parcela_id: int, user) -> Parcela:
        from django.utils import timezone as tz

        parcela = Parcela.objects.select_related(
            "ciclo__contrato__associado"
        ).get(pk=parcela_id)
        if parcela.descartado_em is not None:
            raise ValueError("Inadimplência já foi descartada anteriormente.")
        if parcela.status not in {Parcela.Status.EM_ABERTO, Parcela.Status.NAO_DESCONTADO}:
            raise ValueError(
                f"Não é possível descartar parcela com status '{parcela.status}'."
            )
        if BaixaManualService._can_remove_manual_inadimplencia(parcela):
            parcela.descartado_em = tz.now()
            parcela.descartado_por = user
            parcela.status = Parcela.Status.CANCELADO
            parcela.save(
                update_fields=[
                    "descartado_em",
                    "descartado_por",
                    "status",
                    "updated_at",
                ]
            )
            parcela.soft_delete()
            return parcela
        parcela.descartado_em = tz.now()
        parcela.descartado_por = user
        parcela.save(update_fields=["descartado_em", "descartado_por"])
        return parcela

    @staticmethod
    def listar_parcelas_quitadas(
        search: str | None = None,
        competencia: date | None = None,
        data_inicio: date | None = None,
        data_fim: date | None = None,
        agente: str | None = None,
    ):
        queryset = (
            BaixaManual.objects.select_related(
                "realizado_por",
                "parcela__ciclo__contrato__associado",
                "parcela__ciclo__contrato__agente",
            )
            .filter(
                parcela__ciclo__contrato__contrato_canonico__isnull=True,
            )
            .order_by("-data_baixa", "-created_at", "-id")
        )

        if competencia:
            queryset = queryset.filter(
                parcela__referencia_mes__year=competencia.year,
                parcela__referencia_mes__month=competencia.month,
            )

        if data_inicio:
            queryset = queryset.filter(data_baixa__gte=data_inicio)

        if data_fim:
            queryset = queryset.filter(data_baixa__lte=data_fim)

        queryset = BaixaManualService._apply_baixa_agent_filter(queryset, agente)
        rows: list[dict[str, object] | BaixaManual] = list(
            BaixaManualService._apply_baixa_search(queryset, search)
        )

        pagamentos = (
            PagamentoMensalidade.objects.select_related("associado__agente_responsavel", "manual_by")
            .filter(
                manual_status=PagamentoMensalidade.ManualStatus.PAGO,
                associado__isnull=False,
            )
            .order_by("-manual_paid_at", "-updated_at", "-id")
        )
        if competencia:
            pagamentos = pagamentos.filter(
                referencia_month__year=competencia.year,
                referencia_month__month=competencia.month,
            )
        if data_inicio:
            pagamentos = pagamentos.filter(manual_paid_at__date__gte=data_inicio)
        if data_fim:
            pagamentos = pagamentos.filter(manual_paid_at__date__lte=data_fim)

        agent_filter = DespesaService._build_agent_filter(agente)
        for pagamento in pagamentos:
            if BaixaManualService._manual_payment_has_related_parcela(pagamento):
                continue
            if not DespesaService._matches_pagamento_mensalidade_agent(
                pagamento,
                agent_filter,
            ):
                continue
            row = BaixaManualService._build_quitado_row_from_manual_payment(pagamento)
            if row is None or not BaixaManualService._pending_row_matches_search(row, search):
                continue
            rows.append(row)

        rows.sort(
            key=lambda item: (
                item.get("data_baixa") if isinstance(item, dict) else item.data_baixa,
                item.get("id") if isinstance(item, dict) else item.id,
            ),
            reverse=True,
        )
        return rows

    @staticmethod
    def kpis_pendentes(
        *,
        status_filter: str | None = None,
        competencia: date | None = None,
        data_inicio: date | None = None,
        data_fim: date | None = None,
        agente: str | None = None,
    ) -> dict:
        hoje = timezone.localdate()
        rows = BaixaManualService.listar_parcelas_pendentes(
            status_filter=status_filter,
            competencia=competencia,
            data_inicio=data_inicio,
            data_fim=data_fim,
            agente=agente,
        )
        total_pendentes = len(rows)
        em_aberto = sum(1 for row in rows if row.get("status") == Parcela.Status.EM_ABERTO)
        nao_descontado = sum(
            1 for row in rows if row.get("status") == Parcela.Status.NAO_DESCONTADO
        )
        valor_total = sum(Decimal(str(row.get("valor") or "0")) for row in rows)
        total_associados = len(
            {
                int(associado_id)
                for associado_id in (row.get("associado_id") for row in rows)
                if associado_id is not None
            }
        )
        total_com_parcela = sum(1 for row in rows if row.get("parcela_id"))

        baixas_mes = BaixaManual.objects.filter(
            created_at__year=hoje.year,
            created_at__month=hoje.month,
        ).count()

        total_quitados = BaixaManual.objects.filter(
            parcela__ciclo__contrato__contrato_canonico__isnull=True,
        ).count()

        return {
            "total_pendentes": total_pendentes,
            "total_pendentes_com_parcela": total_com_parcela,
            "em_aberto": em_aberto,
            "nao_descontado": nao_descontado,
            "valor_total_pendente": str(valor_total),
            "baixas_realizadas_mes": baixas_mes,
            "total_associados": total_associados,
            "total_quitados": total_quitados,
            "total_inadimplentes": total_associados,
        }

    @staticmethod
    def kpis_quitados(
        *,
        competencia: date | None = None,
        data_inicio: date | None = None,
        data_fim: date | None = None,
        agente: str | None = None,
    ) -> dict:
        hoje = timezone.localdate()
        rows = BaixaManualService.listar_parcelas_quitadas(
            competencia=competencia,
            data_inicio=data_inicio,
            data_fim=data_fim,
            agente=agente,
        )
        total_quitados = len(rows)
        valor_total_quitado = sum(
            (
                Decimal(str(
                    row.get("valor_pago") if isinstance(row, dict) else row.valor_pago
                ))
                for row in rows
            ),
            Decimal("0.00"),
        )
        quitados_mes_rows = [
            row
            for row in rows
            if (
                (row.get("data_baixa") if isinstance(row, dict) else row.data_baixa).year == hoje.year
                and (row.get("data_baixa") if isinstance(row, dict) else row.data_baixa).month == hoje.month
            )
        ]
        return {
            "total_quitados": total_quitados,
            "valor_total_quitado": str(valor_total_quitado),
            "quitados_este_mes": len(quitados_mes_rows),
            "valor_quitado_este_mes": str(
                sum(
                    (
                        Decimal(str(
                            row.get("valor_pago") if isinstance(row, dict) else row.valor_pago
                        ))
                        for row in quitados_mes_rows
                    ),
                    Decimal("0.00"),
                )
            ),
        }

    @staticmethod
    def _registrar_baixa_manual(
        parcela: Parcela,
        *,
        comprovante,
        valor_pago,
        observacao: str,
        user,
    ) -> BaixaManual:
        if parcela.status not in {
            Parcela.Status.EM_ABERTO,
            Parcela.Status.EM_PREVISAO,
            Parcela.Status.NAO_DESCONTADO,
        }:
            raise ValidationError(
                "Apenas parcelas em aberto, não descontadas ou em previsão podem receber baixa manual."
            )

        if BaixaManual.objects.filter(parcela=parcela).exists():
            raise ValidationError("Esta parcela já possui uma baixa manual registrada.")

        parcela.status = Parcela.Status.DESCONTADO
        parcela.data_pagamento = timezone.localdate()
        if observacao:
            parcela.observacao = observacao
        parcela.save(update_fields=["status", "data_pagamento", "observacao", "updated_at"])
        propagate_competencia_status(parcela)
        rebuild_contract_cycle_state(parcela.ciclo.contrato, execute=True)

        return BaixaManual.objects.create(
            parcela=parcela,
            realizado_por=user,
            comprovante=comprovante,
            nome_comprovante=getattr(comprovante, "name", ""),
            observacao=observacao or "",
            valor_pago=valor_pago,
            data_baixa=timezone.localdate(),
        )

    @staticmethod
    @transaction.atomic
    def dar_baixa(item_id: int, comprovante, valor_pago, observacao: str, user):
        if item_id < 0:
            try:
                item = ArquivoRetornoItem.objects.select_related(
                    "arquivo_retorno",
                    "associado__agente_responsavel",
                    "parcela",
                ).get(pk=abs(item_id))
            except ArquivoRetornoItem.DoesNotExist as exc:
                raise ValidationError("Item do retorno não encontrado.") from exc

            if item.parcela_id:
                return BaixaManualService._registrar_baixa_manual(
                    item.parcela,
                    comprovante=comprovante,
                    valor_pago=valor_pago,
                    observacao=observacao,
                    user=user,
                )

            return BaixaManualService._register_manual_payment_without_parcela(
                item,
                comprovante=comprovante,
                valor_pago=valor_pago,
                observacao=observacao,
                user=user,
            )

        try:
            parcela = Parcela.objects.select_related(
                "ciclo__contrato__associado",
            ).get(pk=item_id)
        except Parcela.DoesNotExist as exc:
            raise ValidationError("Parcela não encontrada.") from exc

        return BaixaManualService._registrar_baixa_manual(
            parcela,
            comprovante=comprovante,
            valor_pago=valor_pago,
            observacao=observacao,
            user=user,
        )

    @staticmethod
    @transaction.atomic
    def inativar_associado_com_baixa(
        associado_id: int,
        *,
        comprovante=None,
        observacao: str,
        user,
    ) -> dict[str, object]:
        try:
            associado = Associado.objects.get(pk=associado_id)
        except Associado.DoesNotExist as exc:
            raise ValidationError("Associado não encontrado.") from exc

        baixas: list[BaixaManual] = []

        if comprovante is not None:
            hoje = timezone.localdate()
            mes_atual = hoje.replace(day=1)
            parcelas = list(
                Parcela.objects.select_related("ciclo__contrato__associado")
                .filter(
                    associado=associado,
                    ciclo__contrato__contrato_canonico__isnull=True,
                    status__in=[Parcela.Status.EM_ABERTO, Parcela.Status.NAO_DESCONTADO],
                    referencia_mes__lt=mes_atual,
                )
                .order_by("referencia_mes", "numero", "id")
            )

            file_name = getattr(comprovante, "name", "comprovante.pdf")
            file_content = comprovante.read()
            if not file_content:
                raise ValidationError("Envie um comprovante válido para registrar a baixa.")

            for index, parcela in enumerate(parcelas, start=1):
                cloned = ContentFile(
                    file_content,
                    name=f"{index:02d}-{parcela.id}-{file_name}",
                )
                baixas.append(
                    BaixaManualService._registrar_baixa_manual(
                        parcela,
                        comprovante=cloned,
                        valor_pago=parcela.valor,
                        observacao=observacao,
                        user=user,
                    )
                )

        associado.status = Associado.Status.INATIVO
        associado.save(update_fields=["status", "updated_at"])

        return {
            "associado_id": associado.id,
            "parcelas_baixadas": len(baixas),
            "total_baixado": str(sum(baixa.valor_pago for baixa in baixas)),
        }


class DespesaService:
    @staticmethod
    def _pagamento_receita_identity(
        pagamento: PagamentoMensalidade,
        origem: str,
    ) -> tuple[str, object, date]:
        associado_key = pagamento.associado_id or re.sub(r"\D", "", pagamento.cpf_cnpj or "")
        return origem, associado_key, pagamento.referencia_month

    @staticmethod
    def _pagamento_receita_recency_key(
        pagamento: PagamentoMensalidade,
    ) -> tuple[object, int]:
        return (
            pagamento.manual_paid_at or pagamento.updated_at or pagamento.created_at,
            pagamento.id,
        )

    @classmethod
    def _listar_receitas_pagamentos(
        cls,
        *,
        start: date,
        end: date,
        agent_filter: dict[str, object] | None = None,
    ) -> list[tuple[str, PagamentoMensalidade]]:
        pagamentos = list(
            PagamentoMensalidade.objects.filter(
                referencia_month__gte=start,
                referencia_month__lt=end,
            )
            .select_related("associado", "associado__agente_responsavel")
            .order_by("referencia_month", "id")
        )
        if agent_filter is not None:
            pagamentos = [
                pagamento
                for pagamento in pagamentos
                if cls._matches_pagamento_mensalidade_agent(
                    pagamento,
                    agent_filter,
                )
            ]

        eventos: dict[tuple[str, object, date], PagamentoMensalidade] = {}
        for pagamento in pagamentos:
            if pagamento.manual_status == PagamentoMensalidade.ManualStatus.PAGO:
                key = cls._pagamento_receita_identity(pagamento, "inadimplencia_manual")
                atual = eventos.get(key)
                if atual is None or cls._pagamento_receita_recency_key(
                    pagamento
                ) >= cls._pagamento_receita_recency_key(atual):
                    eventos[key] = pagamento

            if (pagamento.status_code or "").strip() in {"1", "4"}:
                key = cls._pagamento_receita_identity(pagamento, "arquivo_retorno")
                atual = eventos.get(key)
                if atual is None or cls._pagamento_receita_recency_key(
                    pagamento
                ) >= cls._pagamento_receita_recency_key(atual):
                    eventos[key] = pagamento

        return sorted(
            ((key[0], pagamento) for key, pagamento in eventos.items()),
            key=lambda item: (
                item[1].referencia_month,
                item[1].nome_relatorio or "",
                item[1].id,
                item[0],
            ),
        )

    _RENEWAL_ID_PATTERN = re.compile(r"refi\s*#(\d+)", re.IGNORECASE)

    @staticmethod
    def _monthly_window(month: date | None = None) -> tuple[date, date]:
        month_start = (month or timezone.localdate()).replace(day=1)
        return month_start, shift_month(month_start, 1)

    @staticmethod
    def _manual_launch_reference_date(despesa: Despesa) -> date:
        return despesa.data_pagamento or despesa.data_despesa

    @staticmethod
    def _build_agent_filter(agente: str | None) -> dict[str, object] | None:
        term = (agente or "").strip()
        if not term:
            return None

        agent_id = int(term) if term.isdigit() else None
        resolved_term = term
        if agent_id is not None:
            agent = User.objects.filter(pk=agent_id).only("first_name", "last_name", "email").first()
            if agent is not None:
                resolved_term = agent.full_name or agent.email or term

        return {
            "id": agent_id,
            "term": resolved_term.casefold(),
        }

    @staticmethod
    def _matches_agent_filter(
        agent_filter: dict[str, object] | None,
        *,
        user: User | None = None,
        fallback_text: str | None = None,
    ) -> bool:
        if agent_filter is None:
            return True

        agent_id = agent_filter.get("id")
        if user is not None:
            if agent_id is not None and user.id == agent_id:
                return True

            full_name = (user.full_name or "").casefold()
            email = (user.email or "").casefold()
            term = str(agent_filter["term"])
            if term and (term in full_name or term in email):
                return True

        if fallback_text:
            return str(agent_filter["term"]) in fallback_text.casefold()

        return False

    @classmethod
    def _matches_pagamento_mensalidade_agent(
        cls,
        pagamento: PagamentoMensalidade,
        agent_filter: dict[str, object] | None,
    ) -> bool:
        associado = getattr(pagamento, "associado", None)
        agente = getattr(associado, "agente_responsavel", None)
        return cls._matches_agent_filter(agent_filter, user=agente)

    @classmethod
    def _matches_devolucao_agent(
        cls,
        devolucao: DevolucaoAssociado,
        agent_filter: dict[str, object] | None,
    ) -> bool:
        contrato = getattr(devolucao, "contrato", None)
        agente = getattr(contrato, "agente", None)
        return cls._matches_agent_filter(
            agent_filter,
            user=agente,
            fallback_text=devolucao.agente_snapshot,
        )

    @classmethod
    def _matches_pagamento_operacional_agent(
        cls,
        pagamento: Pagamento,
        pagamento_context: dict[str, object],
        agent_filter: dict[str, object] | None,
    ) -> bool:
        contrato = pagamento_context.get("contrato")
        agente = getattr(contrato, "agente", None) if contrato is not None else None
        return cls._matches_agent_filter(
            agent_filter,
            user=agente,
            fallback_text=pagamento.agente_responsavel,
        )

    @classmethod
    def _resolve_pagamento_operacional_context(
        cls,
        pagamentos: list[Pagamento],
    ) -> dict[int, dict[str, object]]:
        zero = Decimal("0.00")
        contract_ids: set[int] = set()
        contract_codes: set[str] = set()
        refinancing_ids: set[int] = set()
        parsed_refs: dict[int, dict[str, int | str | None]] = {}

        for pagamento in pagamentos:
            refs = (
                pagamento.referencias_externas
                if isinstance(pagamento.referencias_externas, dict)
                else {}
            )
            payment_kind = str(refs.get("payment_kind") or "").strip()
            contract_id_raw = refs.get("contrato_id")
            refinancing_id_raw = refs.get("refinanciamento_id")

            contract_id = (
                int(contract_id_raw)
                if str(contract_id_raw).isdigit()
                else None
            )
            refinancing_id = (
                int(refinancing_id_raw)
                if str(refinancing_id_raw).isdigit()
                else None
            )

            if refinancing_id is None:
                match = cls._RENEWAL_ID_PATTERN.search(pagamento.notes or "")
                if match:
                    refinancing_id = int(match.group(1))
                    if not payment_kind:
                        payment_kind = "renovacao"

            if contract_id is not None:
                contract_ids.add(contract_id)
            elif pagamento.contrato_codigo:
                contract_codes.add(pagamento.contrato_codigo)

            if refinancing_id is not None:
                refinancing_ids.add(refinancing_id)

            parsed_refs[pagamento.id] = {
                "payment_kind": payment_kind or None,
                "contract_id": contract_id,
                "refinancing_id": refinancing_id,
            }

        contracts = Contrato.objects.select_related("agente").filter(
            Q(id__in=contract_ids) | Q(codigo__in=contract_codes)
        )
        contracts_by_id = {contrato.id: contrato for contrato in contracts}
        contracts_by_code = {contrato.codigo: contrato for contrato in contracts}

        refinancings = Refinanciamento.objects.select_related("contrato_origem").filter(
            id__in=refinancing_ids
        )
        refinancings_by_id = {refinanciamento.id: refinanciamento for refinanciamento in refinancings}

        payload: dict[int, dict[str, object]] = {}
        for pagamento in pagamentos:
            refs = parsed_refs.get(pagamento.id, {})
            refinancing = refinancings_by_id.get(int(refs["refinancing_id"])) if refs.get("refinancing_id") else None
            contract = None
            if refinancing and refinancing.contrato_origem_id:
                contract = refinancing.contrato_origem
            elif refs.get("contract_id"):
                contract = contracts_by_id.get(int(refs["contract_id"]))
            elif pagamento.contrato_codigo:
                contract = contracts_by_code.get(pagamento.contrato_codigo)

            payment_kind = str(refs.get("payment_kind") or "").strip()
            notes_lower = (pagamento.notes or "").lower()
            is_renewal = bool(
                payment_kind == "renovacao"
                or refinancing is not None
                or ("renova" in notes_lower and "tesouraria" in notes_lower)
            )

            valor_associado = Decimal(str(pagamento.valor_pago or zero))
            if is_renewal:
                valor_agente = Decimal(
                    str(refinancing.repasse_agente if refinancing is not None else zero)
                )
            else:
                valor_agente = Decimal(
                    str(contract.comissao_agente if contract is not None else zero)
                )

            payload[pagamento.id] = {
                "payment_kind": "renovacao" if is_renewal else "contrato_inicial",
                "contrato": contract,
                "refinanciamento": refinancing,
                "valor_associado": valor_associado,
                "valor_agente": valor_agente,
                "valor_total": valor_associado + valor_agente,
            }

        return payload

    @staticmethod
    def _build_resultado_row(
        *,
        month: date,
        receitas_inadimplencia: Decimal,
        receitas_retorno: Decimal,
        complementos_receita: Decimal,
        despesas_manuais: Decimal,
        devolucoes: Decimal,
        pagamentos_operacionais: Decimal,
    ) -> dict[str, object]:
        receitas = receitas_inadimplencia + receitas_retorno + complementos_receita
        despesas_total = despesas_manuais + devolucoes
        lucro = receitas - despesas_total
        lucro_liquido = lucro - pagamentos_operacionais
        return {
            "mes": month,
            "receitas": receitas,
            "receitas_inadimplencia": receitas_inadimplencia,
            "receitas_retorno": receitas_retorno,
            "complementos_receita": complementos_receita,
            "despesas": despesas_total,
            "despesas_manuais": despesas_manuais,
            "devolucoes": devolucoes,
            "pagamentos_operacionais": pagamentos_operacionais,
            "lucro": lucro,
            "lucro_liquido": lucro_liquido,
        }

    @staticmethod
    def listar_despesas(
        *,
        competencia: date | None = None,
        search: str | None = None,
        status: str | None = None,
        status_anexo: str | None = None,
        tipo: str | None = None,
        natureza: str | None = None,
    ):
        queryset = Despesa.objects.select_related("user").order_by(
            "-data_despesa",
            "-created_at",
            "-id",
        )

        if competencia:
            queryset = queryset.filter(
                data_despesa__year=competencia.year,
                data_despesa__month=competencia.month,
            )

        if search:
            queryset = queryset.filter(
                Q(categoria__icontains=search) | Q(descricao__icontains=search)
            )

        if status in {choice[0] for choice in Despesa.Status.choices}:
            queryset = queryset.filter(status=status)

        if status_anexo in {choice[0] for choice in Despesa.StatusAnexo.choices}:
            queryset = queryset.filter(status_anexo=status_anexo)

        if tipo in {choice[0] for choice in Despesa.Tipo.choices}:
            queryset = queryset.filter(tipo=tipo)

        if natureza in {choice[0] for choice in Despesa.Natureza.choices}:
            queryset = queryset.filter(natureza=natureza)

        return queryset

    @staticmethod
    def kpis(queryset) -> dict[str, object]:
        zero = Value(Decimal("0.00"), output_field=DecimalField(max_digits=15, decimal_places=2))
        return queryset.aggregate(
            total_despesas=Count("id"),
            valor_total=Coalesce(Sum("valor"), zero),
            valor_pago=Coalesce(
                Sum("valor", filter=Q(status=Despesa.Status.PAGO)),
                zero,
            ),
            valor_pendente=Coalesce(
                Sum("valor", filter=Q(status=Despesa.Status.PENDENTE)),
                zero,
            ),
            pendentes_anexo=Count(
                "id",
                filter=Q(status_anexo=Despesa.StatusAnexo.PENDENTE),
            ),
        )

    @staticmethod
    def anexar(despesa: Despesa, arquivo) -> Despesa:
        despesa.anexo = arquivo
        despesa.nome_anexo = getattr(arquivo, "name", "")[:255]
        despesa.status_anexo = Despesa.StatusAnexo.ANEXADO
        despesa.save(update_fields=["anexo", "nome_anexo", "status_anexo", "updated_at"])
        return despesa

    @staticmethod
    def excluir(despesa: Despesa) -> None:
        despesa.soft_delete()

    @staticmethod
    def sugerir_categorias(search: str | None = None) -> list[dict[str, object]]:
        queryset = (
            Despesa.objects.exclude(categoria="")
            .values("categoria")
            .annotate(total=Count("id"))
            .order_by("-total", "categoria")
        )
        if search:
            queryset = queryset.filter(categoria__icontains=search)
        return [
            {"categoria": row["categoria"], "frequencia": row["total"]}
            for row in queryset[:12]
        ]

    @staticmethod
    def resultado_mensal(
        *,
        competencia: date | None = None,
        agente: str | None = None,
    ) -> dict[str, object]:
        base_month = (competencia or timezone.localdate()).replace(day=1)
        start_month = shift_month(base_month, -11)
        months = [shift_month(start_month, index) for index in range(12)]
        _, end_month = DespesaService._monthly_window(base_month)
        zero = Decimal("0.00")
        agent_filter = DespesaService._build_agent_filter(agente)

        pagamentos = DespesaService._listar_receitas_pagamentos(
            start=start_month,
            end=end_month,
            agent_filter=agent_filter,
        )
        despesas = list(
            Despesa.objects.filter(
                Q(data_despesa__gte=start_month, data_despesa__lt=end_month)
                | Q(data_pagamento__gte=start_month, data_pagamento__lt=end_month)
            )
        )
        if agent_filter is not None:
            despesas = []
        devolucoes = list(
            DevolucaoAssociado.objects.filter(
                revertida_em__isnull=True,
                data_devolucao__gte=start_month,
                data_devolucao__lt=end_month,
            )
            .select_related("contrato", "contrato__agente")
        )
        if agent_filter is not None:
            devolucoes = [
                devolucao
                for devolucao in devolucoes
                if DespesaService._matches_devolucao_agent(devolucao, agent_filter)
            ]
        pagamentos_operacionais = list(
            Pagamento.objects.filter(
                status=Pagamento.Status.PAGO,
                paid_at__date__gte=start_month,
                paid_at__date__lt=end_month,
            )
        )

        receitas_inadimplencia_por_mes = {month: zero for month in months}
        receitas_retorno_por_mes = {month: zero for month in months}
        despesas_manuais_por_mes = {month: zero for month in months}
        complementos_receita_por_mes = {month: zero for month in months}
        devolucoes_por_mes = {month: zero for month in months}
        pagamentos_operacionais_por_mes = {month: zero for month in months}

        for origem, pagamento in pagamentos:
            month_key = pagamento.referencia_month.replace(day=1)
            if month_key not in receitas_inadimplencia_por_mes:
                continue
            if origem == "inadimplencia_manual":
                receitas_inadimplencia_por_mes[month_key] += Decimal(
                    str(pagamento.recebido_manual or pagamento.valor or zero)
                )
            elif origem == "arquivo_retorno":
                receitas_retorno_por_mes[month_key] += Decimal(
                    str(pagamento.valor or zero)
                )

        for despesa in despesas:
            if despesa.natureza == Despesa.Natureza.COMPLEMENTO_RECEITA:
                if despesa.status != Despesa.Status.PAGO:
                    continue
                month_key = DespesaService._manual_launch_reference_date(despesa).replace(day=1)
                if month_key in complementos_receita_por_mes:
                    complementos_receita_por_mes[month_key] += Decimal(str(despesa.valor or zero))
                continue

            month_key = despesa.data_despesa.replace(day=1)
            if month_key in despesas_manuais_por_mes:
                despesas_manuais_por_mes[month_key] += Decimal(str(despesa.valor or zero))

        for devolucao in devolucoes:
            month_key = devolucao.data_devolucao.replace(day=1)
            if month_key in devolucoes_por_mes:
                devolucoes_por_mes[month_key] += Decimal(str(devolucao.valor or zero))

        pagamentos_operacionais_context = DespesaService._resolve_pagamento_operacional_context(
            pagamentos_operacionais
        )
        if agent_filter is not None:
            pagamentos_operacionais = [
                pagamento
                for pagamento in pagamentos_operacionais
                if DespesaService._matches_pagamento_operacional_agent(
                    pagamento,
                    pagamentos_operacionais_context.get(pagamento.id, {}),
                    agent_filter,
                )
            ]
        for pagamento in pagamentos_operacionais:
            if pagamento.paid_at is None:
                continue
            month_key = pagamento.paid_at.date().replace(day=1)
            if month_key in pagamentos_operacionais_por_mes:
                pagamento_context = pagamentos_operacionais_context.get(pagamento.id, {})
                pagamentos_operacionais_por_mes[month_key] += Decimal(
                    str(pagamento_context.get("valor_total") or zero)
                )

        rows: list[dict[str, object]] = []
        for month in months:
            rows.append(
                DespesaService._build_resultado_row(
                    month=month,
                    receitas_inadimplencia=receitas_inadimplencia_por_mes[month],
                    receitas_retorno=receitas_retorno_por_mes[month],
                    complementos_receita=complementos_receita_por_mes[month],
                    despesas_manuais=despesas_manuais_por_mes[month],
                    devolucoes=devolucoes_por_mes[month],
                    pagamentos_operacionais=pagamentos_operacionais_por_mes[month],
                )
            )

        totais = {
            "receitas": sum((row["receitas"] for row in rows), zero),
            "despesas": sum((row["despesas"] for row in rows), zero),
            "lucro": sum((row["lucro"] for row in rows), zero),
            "lucro_liquido": sum((row["lucro_liquido"] for row in rows), zero),
        }
        return {"rows": rows, "totais": totais}

    @staticmethod
    def resultado_mensal_detalhe(
        *,
        mes: date,
        agente: str | None = None,
    ) -> dict[str, object]:
        month_start, next_month = DespesaService._monthly_window(mes)
        zero = Decimal("0.00")
        agent_filter = DespesaService._build_agent_filter(agente)

        pagamentos = DespesaService._listar_receitas_pagamentos(
            start=month_start,
            end=next_month,
            agent_filter=agent_filter,
        )
        despesas = list(
            Despesa.objects.filter(
                Q(data_despesa__gte=month_start, data_despesa__lt=next_month)
                | Q(data_pagamento__gte=month_start, data_pagamento__lt=next_month)
            )
            .select_related("user")
            .order_by("-data_despesa", "-created_at", "-id")
        )
        if agent_filter is not None:
            despesas = []
        devolucoes = list(
            DevolucaoAssociado.objects.filter(
                revertida_em__isnull=True,
                data_devolucao__gte=month_start,
                data_devolucao__lt=next_month,
            )
            .select_related("associado", "contrato", "contrato__agente", "realizado_por")
            .order_by("-data_devolucao", "-created_at", "-id")
        )
        if agent_filter is not None:
            devolucoes = [
                devolucao
                for devolucao in devolucoes
                if DespesaService._matches_devolucao_agent(devolucao, agent_filter)
            ]
        pagamentos_operacionais = list(
            Pagamento.objects.filter(
                status=Pagamento.Status.PAGO,
                paid_at__date__gte=month_start,
                paid_at__date__lt=next_month,
            )
            .select_related("cadastro")
            .order_by("-paid_at", "-created_at", "-id")
        )

        receitas_itens: list[dict[str, object]] = []
        receitas_inadimplencia = zero
        receitas_retorno = zero
        complementos_receita = zero

        for origem, pagamento in pagamentos:
            associado = pagamento.associado
            associado_nome = (
                associado.nome_completo
                if associado and associado.nome_completo
                else pagamento.nome_relatorio or "-"
            )
            matricula = ""
            agente_nome = ""
            if associado:
                matricula = associado.matricula_orgao or associado.matricula or pagamento.matricula
                if associado.agente_responsavel:
                    agente_nome = associado.agente_responsavel.full_name

            if origem == "inadimplencia_manual":
                valor = Decimal(str(pagamento.recebido_manual or pagamento.valor or zero))
                receitas_inadimplencia += valor
                receitas_itens.append(
                    {
                        "id": pagamento.id,
                        "origem": "inadimplencia_manual",
                        "origem_label": "Inadimplência manual",
                        "data": (
                            pagamento.manual_paid_at.date()
                            if pagamento.manual_paid_at
                            else pagamento.referencia_month
                        ),
                        "referencia": pagamento.referencia_month,
                        "associado_nome": associado_nome,
                        "cpf_cnpj": pagamento.cpf_cnpj,
                        "matricula": matricula,
                        "agente_nome": agente_nome,
                        "descricao": "Recebimento manual de inadimplência.",
                        "valor": valor,
                    }
                )
            elif origem == "arquivo_retorno":
                valor = Decimal(str(pagamento.valor or zero))
                receitas_retorno += valor
                receitas_itens.append(
                    {
                        "id": pagamento.id,
                        "origem": "arquivo_retorno",
                        "origem_label": "Arquivo retorno",
                        "data": pagamento.referencia_month,
                        "referencia": pagamento.referencia_month,
                        "associado_nome": associado_nome,
                        "cpf_cnpj": pagamento.cpf_cnpj,
                        "matricula": matricula,
                        "agente_nome": agente_nome,
                        "descricao": "Receita reconhecida via arquivo retorno.",
                        "valor": valor,
                    }
                )

        despesas_itens: list[dict[str, object]] = []
        despesas_manuais = zero
        devolucoes_total = zero

        for despesa in despesas:
            valor = Decimal(str(despesa.valor or zero))
            if despesa.natureza == Despesa.Natureza.COMPLEMENTO_RECEITA:
                if despesa.status != Despesa.Status.PAGO:
                    continue
                reference_date = DespesaService._manual_launch_reference_date(despesa)
                if reference_date < month_start or reference_date >= next_month:
                    continue
                complementos_receita += valor
                receitas_itens.append(
                    {
                        "id": despesa.id,
                        "origem": "complemento_receita",
                        "origem_label": "Complemento de receita",
                        "data": reference_date,
                        "referencia": reference_date.replace(day=1),
                        "associado_nome": despesa.categoria,
                        "cpf_cnpj": "",
                        "matricula": "",
                        "agente_nome": "",
                        "descricao": despesa.descricao or "Complemento de receita lançado manualmente.",
                        "valor": valor,
                    }
                )
                continue

            despesas_manuais += valor
            despesas_itens.append(
                {
                    "id": despesa.id,
                    "origem": "despesa_manual",
                    "origem_label": "Despesa manual",
                    "data": despesa.data_despesa,
                    "titulo": despesa.categoria,
                    "subtitulo": despesa.descricao or "Sem descrição complementar.",
                    "descricao": despesa.observacoes or "",
                    "referencia": (
                        f"{despesa.get_status_display()} · {despesa.get_tipo_display()}"
                        if despesa.tipo
                        else despesa.get_status_display()
                    ),
                    "valor": valor,
                }
            )

        for devolucao in devolucoes:
            valor = Decimal(str(devolucao.valor or zero))
            devolucoes_total += valor
            despesas_itens.append(
                {
                    "id": devolucao.id,
                    "origem": "devolucao",
                    "origem_label": "Devolução",
                    "data": devolucao.data_devolucao,
                    "titulo": devolucao.nome_snapshot,
                    "subtitulo": devolucao.contrato_codigo_snapshot,
                    "descricao": devolucao.motivo,
                    "referencia": devolucao.get_tipo_display(),
                    "valor": valor,
                }
            )

        pagamentos_operacionais_itens: list[dict[str, object]] = []
        pagamentos_operacionais_total = zero
        pagamentos_operacionais_context = DespesaService._resolve_pagamento_operacional_context(
            pagamentos_operacionais
        )
        if agent_filter is not None:
            pagamentos_operacionais = [
                pagamento
                for pagamento in pagamentos_operacionais
                if DespesaService._matches_pagamento_operacional_agent(
                    pagamento,
                    pagamentos_operacionais_context.get(pagamento.id, {}),
                    agent_filter,
                )
            ]

        for pagamento in pagamentos_operacionais:
            if pagamento.paid_at is None:
                continue
            pagamento_context = pagamentos_operacionais_context.get(pagamento.id, {})
            valor_associado = Decimal(str(pagamento_context.get("valor_associado") or zero))
            valor_agente = Decimal(str(pagamento_context.get("valor_agente") or zero))
            valor_total = Decimal(str(pagamento_context.get("valor_total") or zero))
            pagamentos_operacionais_total += valor_total
            pagamentos_operacionais_itens.append(
                {
                    "id": pagamento.id,
                    "data": pagamento.paid_at.date(),
                    "favorecido": pagamento.full_name,
                    "cpf_cnpj": pagamento.cpf_cnpj,
                    "agente_nome": pagamento.agente_responsavel,
                    "contrato_codigo": pagamento.contrato_codigo,
                    "origem": pagamento.origem,
                    "origem_label": pagamento.get_origem_display(),
                    "valor_associado": valor_associado,
                    "valor_agente": valor_agente,
                    "valor_total": valor_total,
                }
            )

        resumo = DespesaService._build_resultado_row(
            month=month_start,
            receitas_inadimplencia=receitas_inadimplencia,
            receitas_retorno=receitas_retorno,
            complementos_receita=complementos_receita,
            despesas_manuais=despesas_manuais,
            devolucoes=devolucoes_total,
            pagamentos_operacionais=pagamentos_operacionais_total,
        )

        return {
            "mes": month_start,
            "resumo": {key: value for key, value in resumo.items() if key != "mes"},
            "receitas": receitas_itens,
            "despesas": despesas_itens,
            "pagamentos_operacionais": pagamentos_operacionais_itens,
        }
