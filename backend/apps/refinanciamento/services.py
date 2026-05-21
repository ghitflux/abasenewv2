from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from django.db import transaction
from django.db.models import DateTimeField, Exists, OuterRef, Q, QuerySet, Subquery
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.associados.models import Associado
from apps.contratos.cycle_projection import build_contract_cycle_projection
from apps.contratos.cycle_rebuild import rebuild_contract_cycle_state
from apps.contratos.cycle_timeline import (
    add_months,
    get_contract_cycle_size,
    get_destination_cycle_start,
)
from apps.contratos.cycle_projection import sync_associado_mother_status
from apps.contratos.models import Ciclo, Contrato, Parcela
from apps.esteira.models import EsteiraItem, Transicao
from apps.importacao.models import PagamentoMensalidade
from apps.tesouraria.models import Pagamento

from .models import Assumption, Comprovante, Item, Refinanciamento
from .strategies import StandardEligibilityStrategy

_OPERATIONAL_ACTIVE_STATUSES = {
    Refinanciamento.Status.APTO_A_RENOVAR,
    Refinanciamento.Status.SOLICITADO_PARA_LIQUIDACAO,
    Refinanciamento.Status.EM_ANALISE_RENOVACAO,
    Refinanciamento.Status.PENDENTE_TERMO_ANALISTA,
    Refinanciamento.Status.PENDENTE_TERMO_AGENTE,
    Refinanciamento.Status.APROVADO_ANALISE_RENOVACAO,
    Refinanciamento.Status.APROVADO_PARA_RENOVACAO,
    Refinanciamento.Status.PENDENTE_APTO,
    Refinanciamento.Status.SOLICITADO,
    Refinanciamento.Status.EM_ANALISE,
    Refinanciamento.Status.APROVADO,
    Refinanciamento.Status.CONCLUIDO,
}


def _normalize_status(status: str) -> str:
    mapping = {
        Refinanciamento.Status.PENDENTE_APTO: Refinanciamento.Status.APTO_A_RENOVAR,
        Refinanciamento.Status.SOLICITADO: Refinanciamento.Status.APTO_A_RENOVAR,
        Refinanciamento.Status.EM_ANALISE: Refinanciamento.Status.EM_ANALISE_RENOVACAO,
        Refinanciamento.Status.APROVADO: Refinanciamento.Status.APROVADO_ANALISE_RENOVACAO,
        Refinanciamento.Status.CONCLUIDO: Refinanciamento.Status.APROVADO_ANALISE_RENOVACAO,
    }
    return mapping.get(status, status)


def _cycle_key(parcelas: list[dict[str, object]]) -> str:
    referencias = [parcela["referencia_mes"] for parcela in parcelas]
    return "|".join(referencia.strftime("%Y-%m") for referencia in referencias)


def _paid_projection_parcelas(parcelas: list[dict[str, object]]) -> list[dict[str, object]]:
    return [
        parcela
        for parcela in parcelas
        if parcela["status"]
        in {Parcela.Status.DESCONTADO, Parcela.Status.LIQUIDADA, "quitada"}
    ]


def renewal_materialization_annotations() -> dict[str, object]:
    linked_payment = Pagamento.all_objects.filter(
        cadastro_id=OuterRef("associado_id"),
        contrato_codigo=OuterRef("contrato_origem__codigo"),
        referencias_externas__refinanciamento_id=OuterRef("pk"),
        deleted_at__isnull=True,
        status=Pagamento.Status.PAGO,
    ).order_by("-paid_at", "-created_at", "-id")
    comprovantes = Comprovante.objects.filter(
        refinanciamento_id=OuterRef("pk"),
        deleted_at__isnull=True,
    )
    associado_payment_proof = comprovantes.filter(
        papel=Comprovante.Papel.ASSOCIADO,
        tipo=Comprovante.Tipo.COMPROVANTE_PAGAMENTO_ASSOCIADO,
    ).order_by("-data_pagamento", "-updated_at", "-created_at", "-id")
    agente_payment_proof = comprovantes.filter(
        papel=Comprovante.Papel.AGENTE,
        tipo=Comprovante.Tipo.COMPROVANTE_PAGAMENTO_AGENTE,
    ).order_by("-data_pagamento", "-updated_at", "-created_at", "-id")
    return {
        "has_linked_renewal_payment": Exists(linked_payment),
        "has_associado_payment_proof": Exists(associado_payment_proof),
        "has_agente_payment_proof": Exists(agente_payment_proof),
        "linked_payment_paid_at": Subquery(
            linked_payment.values("paid_at")[:1],
            output_field=DateTimeField(),
        ),
        "associado_payment_proof_paid_at": Subquery(
            associado_payment_proof.values("data_pagamento")[:1],
            output_field=DateTimeField(),
        ),
        "agente_payment_proof_paid_at": Subquery(
            agente_payment_proof.values("data_pagamento")[:1],
            output_field=DateTimeField(),
        ),
    }


def renewal_materialized_q() -> Q:
    return (
        Q(executado_em__isnull=False)
        | Q(data_ativacao_ciclo__isnull=False)
        | Q(ciclo_destino__isnull=False)
        | Q(has_linked_renewal_payment=True)
        | (
            Q(has_associado_payment_proof=True)
            & Q(has_agente_payment_proof=True)
        )
    )


def annotate_renewal_materialization(
    queryset: QuerySet[Refinanciamento],
) -> QuerySet[Refinanciamento]:
    return queryset.annotate(**renewal_materialization_annotations())


def is_renewal_materialized(refinanciamento: Refinanciamento) -> bool:
    if (
        refinanciamento.executado_em is not None
        or refinanciamento.data_ativacao_ciclo is not None
        or refinanciamento.ciclo_destino_id is not None
    ):
        return True
    linked_payment = RefinanciamentoService._get_renewal_payment(refinanciamento)
    if linked_payment is not None:
        return True
    return bool(
        RefinanciamentoService._latest_payment_comprovante(
            refinanciamento,
            Comprovante.Papel.ASSOCIADO,
        )
        and RefinanciamentoService._latest_payment_comprovante(
            refinanciamento,
            Comprovante.Papel.AGENTE,
        )
    )


class RefinanciamentoService:
    strategy = StandardEligibilityStrategy()

    @staticmethod
    def _get_contrato(contrato_id: int) -> Contrato:
        try:
            return (
                Contrato.objects.select_related("associado", "agente")
                .prefetch_related(
                    "ciclos__parcelas",
                    "associado__refinanciamentos",
                    "associado__pagamentos_mensalidades__refi_itens",
                    "associado__tesouraria_pagamentos",
                )
                .get(pk=contrato_id)
            )
        except Contrato.DoesNotExist as exc:
            raise ValidationError("Contrato não encontrado.") from exc

    @staticmethod
    def _get_refinanciamento(refinanciamento_id: int) -> Refinanciamento:
        try:
            return (
                Refinanciamento.objects.select_related(
                    "associado",
                    "contrato_origem",
                    "contrato_origem__agente",
                    "ciclo_origem",
                    "ciclo_destino",
                    "solicitado_por",
                    "aprovado_por",
                    "bloqueado_por",
                    "efetivado_por",
                    "reviewed_by",
                )
                .prefetch_related(
                    "ciclo_origem__parcelas",
                    "ciclo_destino__parcelas",
                    "comprovantes__enviado_por",
                    "itens__pagamento_mensalidade",
                )
                .get(pk=refinanciamento_id)
            )
        except Refinanciamento.DoesNotExist as exc:
            raise ValidationError("Refinanciamento não encontrado.") from exc

    @staticmethod
    def _payment_tipo_for_papel(papel: str) -> str:
        return (
            Comprovante.Tipo.COMPROVANTE_PAGAMENTO_ASSOCIADO
            if papel == Comprovante.Papel.ASSOCIADO
            else Comprovante.Tipo.COMPROVANTE_PAGAMENTO_AGENTE
        )

    @staticmethod
    def _latest_payment_comprovante(
        refinanciamento: Refinanciamento,
        papel: str,
    ) -> Comprovante | None:
        return (
            refinanciamento.comprovantes.filter(
                deleted_at__isnull=True,
                papel=papel,
                tipo=RefinanciamentoService._payment_tipo_for_papel(papel),
            )
            .order_by("-updated_at", "-created_at", "-id")
            .first()
        )

    @staticmethod
    def _latest_termo_comprovante(
        refinanciamento: Refinanciamento,
    ) -> Comprovante | None:
        return (
            refinanciamento.comprovantes.filter(
                deleted_at__isnull=True,
                tipo=Comprovante.Tipo.TERMO_ANTECIPACAO,
                papel=Comprovante.Papel.AGENTE,
            )
            .order_by("-updated_at", "-created_at", "-id")
            .first()
        )

    @staticmethod
    def _upsert_payment_comprovante(
        refinanciamento: Refinanciamento,
        *,
        papel: str,
        arquivo,
        user,
        data_pagamento: datetime,
    ) -> Comprovante:
        contrato = refinanciamento.contrato_origem
        payload = {
            "refinanciamento": refinanciamento,
            "contrato": contrato,
            "ciclo": refinanciamento.ciclo_destino or refinanciamento.ciclo_origem,
            "tipo": RefinanciamentoService._payment_tipo_for_papel(papel),
            "papel": papel,
            "origem": Comprovante.Origem.TESOURARIA_RENOVACAO,
            "arquivo": arquivo,
            "nome_original": getattr(arquivo, "name", ""),
            "mime": getattr(arquivo, "content_type", "") or "",
            "size_bytes": getattr(arquivo, "size", None),
            "arquivo_referencia_path": "",
            "enviado_por": user,
            "data_pagamento": data_pagamento,
            "agente_snapshot": contrato.agente.full_name if contrato and contrato.agente else "",
        }
        return Comprovante.objects.create(**payload)

    @staticmethod
    def _upsert_termo_comprovante(
        refinanciamento: Refinanciamento,
        *,
        arquivo,
        user,
    ) -> Comprovante:
        contrato = refinanciamento.contrato_origem
        payload = {
            "refinanciamento": refinanciamento,
            "contrato": contrato,
            "ciclo": refinanciamento.ciclo_origem,
            "tipo": Comprovante.Tipo.TERMO_ANTECIPACAO,
            "papel": Comprovante.Papel.AGENTE,
            "origem": Comprovante.Origem.TESOURARIA_RENOVACAO,
            "arquivo": arquivo,
            "nome_original": getattr(arquivo, "name", ""),
            "mime": getattr(arquivo, "content_type", "") or "",
            "size_bytes": getattr(arquivo, "size", None),
            "arquivo_referencia_path": "",
            "enviado_por": user,
            "data_pagamento": None,
            "agente_snapshot": contrato.agente.full_name if contrato and contrato.agente else "",
        }
        return Comprovante.objects.create(**payload)

    @staticmethod
    def _get_renewal_payment(refinanciamento: Refinanciamento) -> Pagamento | None:
        contrato = refinanciamento.contrato_origem
        if contrato is None:
            return None
        return (
            Pagamento.all_objects.filter(
                cadastro=contrato.associado,
                contrato_codigo=contrato.codigo,
                referencias_externas__refinanciamento_id=refinanciamento.id,
            )
            .order_by("created_at", "id")
            .first()
        )

    @staticmethod
    def _sync_renewal_payment_paths(
        pagamento: Pagamento,
        refinanciamento: Refinanciamento,
    ) -> Pagamento:
        comprovante_associado = RefinanciamentoService._latest_payment_comprovante(
            refinanciamento,
            Comprovante.Papel.ASSOCIADO,
        )
        comprovante_agente = RefinanciamentoService._latest_payment_comprovante(
            refinanciamento,
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
    def _upsert_renewal_payment(
        refinanciamento: Refinanciamento,
        *,
        user,
        paid_at: datetime,
    ) -> Pagamento:
        contrato = refinanciamento.contrato_origem
        if contrato is None:
            raise ValidationError("Renovação sem contrato de origem.")

        pagamento = RefinanciamentoService._get_renewal_payment(refinanciamento)
        if pagamento is None:
            pagamento = Pagamento(
                cadastro=contrato.associado,
                created_by=user,
                contrato_codigo=contrato.codigo,
                contrato_valor_antecipacao=refinanciamento.valor_refinanciamento,
                contrato_margem_disponivel=contrato.margem_disponivel,
                cpf_cnpj=contrato.associado.cpf_cnpj,
                full_name=contrato.associado.nome_completo,
                agente_responsavel=contrato.agente.full_name if contrato.agente else "",
                origem=Pagamento.Origem.OPERACIONAL,
                forma_pagamento="pix",
                referencias_externas={
                    "payment_kind": "renovacao",
                    "contrato_id": contrato.id,
                    "refinanciamento_id": refinanciamento.id,
                },
            )

        pagamento.created_by = pagamento.created_by or user
        pagamento.contrato_valor_antecipacao = refinanciamento.valor_refinanciamento
        pagamento.contrato_margem_disponivel = contrato.margem_disponivel
        pagamento.cpf_cnpj = contrato.associado.cpf_cnpj
        pagamento.full_name = contrato.associado.nome_completo
        pagamento.agente_responsavel = contrato.agente.full_name if contrato.agente else ""
        pagamento.origem = Pagamento.Origem.OPERACIONAL
        pagamento.status = Pagamento.Status.PAGO
        pagamento.valor_pago = refinanciamento.valor_refinanciamento
        pagamento.paid_at = paid_at
        pagamento.forma_pagamento = pagamento.forma_pagamento or "pix"
        pagamento.referencias_externas = {
            **(pagamento.referencias_externas or {}),
            "payment_kind": "renovacao",
            "contrato_id": contrato.id,
            "refinanciamento_id": refinanciamento.id,
        }
        pagamento.notes = (
            f"Renovação efetivada via tesouraria para refi #{refinanciamento.id}."
        )
        pagamento = RefinanciamentoService._sync_renewal_payment_paths(
            pagamento,
            refinanciamento,
        )
        pagamento.save()
        return pagamento

    @staticmethod
    def _require_effectivation_comprovantes(
        refinanciamento: Refinanciamento,
    ) -> tuple[Comprovante, Comprovante]:
        comprovante_associado = RefinanciamentoService._latest_payment_comprovante(
            refinanciamento,
            Comprovante.Papel.ASSOCIADO,
        )
        comprovante_agente = RefinanciamentoService._latest_payment_comprovante(
            refinanciamento,
            Comprovante.Papel.AGENTE,
        )
        errors: dict[str, list[str]] = {}
        if comprovante_associado is None:
            errors["comprovante_associado"] = [
                "O comprovante do associado é obrigatório para efetivar a renovação."
            ]
        if comprovante_agente is None:
            errors["comprovante_agente"] = [
                "O comprovante do agente é obrigatório para efetivar a renovação."
            ]
        if errors:
            raise ValidationError(errors)
        return comprovante_associado, comprovante_agente

    @staticmethod
    def _serialize_pagamento_seed(pagamento: PagamentoMensalidade) -> dict[str, object]:
        return {
            "pagamento_mensalidade_id": pagamento.id,
            "referencia_month": pagamento.referencia_month.isoformat(),
            "status_code": pagamento.status_code or "1",
            "valor": str(pagamento.valor or Decimal("0.00")),
            "import_uuid": pagamento.import_uuid,
            "source_file_path": pagamento.source_file_path,
        }

    @staticmethod
    def _registrar_auditoria(contrato: Contrato, user, acao: str, observacao: str):
        esteira_item = getattr(contrato.associado, "esteira_item", None)
        if not esteira_item:
            return

        Transicao.objects.create(
            esteira_item=esteira_item,
            acao=acao,
            de_status=esteira_item.etapa_atual,
            para_status=esteira_item.etapa_atual,
            de_situacao=esteira_item.status,
            para_situacao=esteira_item.status,
            realizado_por=user,
            observacao=observacao,
        )

    @staticmethod
    def _mover_esteira_para_tesouraria(
        contrato: Contrato,
        user,
        *,
        acao: str,
        observacao: str,
    ) -> bool:
        esteira_item = getattr(contrato.associado, "esteira_item", None)
        if not esteira_item:
            return False

        de_etapa = esteira_item.etapa_atual
        de_situacao = esteira_item.status
        if (
            de_etapa == EsteiraItem.Etapa.TESOURARIA
            and de_situacao == EsteiraItem.Situacao.AGUARDANDO
        ):
            return True

        esteira_item.etapa_atual = EsteiraItem.Etapa.TESOURARIA
        esteira_item.status = EsteiraItem.Situacao.AGUARDANDO
        esteira_item.tesoureiro_responsavel = None
        esteira_item.save(
            update_fields=[
                "etapa_atual",
                "status",
                "tesoureiro_responsavel",
                "updated_at",
            ]
        )

        Transicao.objects.create(
            esteira_item=esteira_item,
            acao=acao,
            de_status=de_etapa,
            para_status=EsteiraItem.Etapa.TESOURARIA,
            de_situacao=de_situacao,
            para_situacao=EsteiraItem.Situacao.AGUARDANDO,
            realizado_por=user,
            observacao=observacao,
        )
        return True

    @staticmethod
    def _active_operational_refinanciamento(contrato: Contrato) -> Refinanciamento | None:
        return (
            Refinanciamento.objects.filter(
                contrato_origem=contrato,
                deleted_at__isnull=True,
                legacy_refinanciamento_id__isnull=True,
                origem=Refinanciamento.Origem.OPERACIONAL,
                ciclo_destino__isnull=True,
                data_ativacao_ciclo__isnull=True,
                executado_em__isnull=True,
                status__in=_OPERATIONAL_ACTIVE_STATUSES,
            )
            .order_by("-created_at", "-id")
            .first()
        )

    @staticmethod
    def _reabrir_assumption_para_analise(
        refinanciamento: Refinanciamento,
    ) -> Assumption | None:
        assumption = RefinanciamentoService._assumption_for_refinanciamento(refinanciamento)
        if assumption is None:
            return None

        assumption.status = Assumption.Status.LIBERADO
        assumption.assumido_em = None
        assumption.finalizado_em = None
        assumption.heartbeat_at = None
        assumption.save(
            update_fields=[
                "status",
                "assumido_em",
                "finalizado_em",
                "heartbeat_at",
                "updated_at",
            ]
        )
        return assumption

    @staticmethod
    def _assumption_for_refinanciamento(refinanciamento: Refinanciamento) -> Assumption | None:
        if not refinanciamento.cycle_key:
            return None
        return Assumption.objects.filter(
            cadastro=refinanciamento.associado,
            request_key=refinanciamento.cycle_key,
        ).order_by("-created_at", "-id").first()

    @staticmethod
    def _sync_contract_and_get_refi(contrato: Contrato) -> tuple[dict[str, object], Refinanciamento | None]:
        rebuild_contract_cycle_state(contrato, execute=True)
        contrato.refresh_from_db()
        projection = build_contract_cycle_projection(contrato)
        return projection, RefinanciamentoService._active_operational_refinanciamento(contrato)

    @staticmethod
    def verificar_elegibilidade(contrato_id: int) -> dict[str, object]:
        contrato = RefinanciamentoService._get_contrato(contrato_id)
        return RefinanciamentoService.strategy.evaluate(contrato)

    @staticmethod
    @transaction.atomic
    def solicitar(contrato_id: int, termo_antecipacao, user) -> Refinanciamento:
        contrato = RefinanciamentoService._get_contrato(contrato_id)
        evaluation = RefinanciamentoService.strategy.evaluate(contrato)
        if not evaluation["elegivel"]:
            raise ValidationError(evaluation["motivo"])

        _, refinanciamento = RefinanciamentoService._sync_contract_and_get_refi(
            contrato
        )
        if refinanciamento is None:
            # Fallback: a strategy ja aprovou elegibilidade, mas o rebuild pode
            # ter sido bloqueado por um refi anterior em REVERTIDO/DESATIVADO no
            # mesmo ciclo (has_cancelled_operational_refinanciamento). Forca a
            # materializacao porque o usuario esta explicitamente solicitando.
            rebuild_contract_cycle_state(
                contrato,
                execute=True,
                force_active_operational_status=Refinanciamento.Status.APTO_A_RENOVAR,
            )
            contrato.refresh_from_db()
            refinanciamento = RefinanciamentoService._active_operational_refinanciamento(
                contrato
            )
        if refinanciamento is None:
            raise ValidationError(
                "A fila operacional de renovação não pôde ser materializada."
            )

        normalized_status = _normalize_status(refinanciamento.status)
        reenviado_pelo_agente = (
            refinanciamento.status == Refinanciamento.Status.PENDENTE_TERMO_AGENTE
        )
        if not termo_antecipacao:
            comprovantes_contrato = contrato.comprovantes.filter(
                refinanciamento__isnull=True
            )
            if reenviado_pelo_agente or not comprovantes_contrato.exists():
                raise ValidationError(
                    {"termo_antecipacao": ["Nenhum arquivo foi submetido."]}
                )
        if (
            normalized_status != Refinanciamento.Status.APTO_A_RENOVAR
            and not reenviado_pelo_agente
        ):
            raise ValidationError(
                "Esta renovação já foi enviada para análise ou está em etapa posterior."
            )

        refinanciamento.status = Refinanciamento.Status.EM_ANALISE_RENOVACAO
        refinanciamento.solicitado_por = user
        refinanciamento.reviewed_by = None
        refinanciamento.reviewed_at = None
        if not reenviado_pelo_agente:
            refinanciamento.analista_note = ""
            refinanciamento.coordenador_note = ""
        refinanciamento.termo_antecipacao_path = (
            getattr(termo_antecipacao, "name", "") if termo_antecipacao else ""
        )
        refinanciamento.termo_antecipacao_original_name = (
            getattr(termo_antecipacao, "name", "") if termo_antecipacao else ""
        )
        refinanciamento.termo_antecipacao_mime = (
            (getattr(termo_antecipacao, "content_type", "") or "")
            if termo_antecipacao
            else ""
        )
        refinanciamento.termo_antecipacao_size_bytes = (
            getattr(termo_antecipacao, "size", None) if termo_antecipacao else None
        )
        refinanciamento.termo_antecipacao_uploaded_at = (
            timezone.now() if termo_antecipacao else None
        )
        refinanciamento.observacao = (
            "Agente reenviou o termo de antecipação para nova análise."
            if reenviado_pelo_agente
            else (
                "Agente anexou o termo de antecipação e enviou a renovação para análise."
                if termo_antecipacao
                else "Agente solicitou a renovação para análise."
            )
        )
        refinanciamento.save(
            update_fields=[
                "status",
                "solicitado_por",
                "reviewed_by",
                "reviewed_at",
                "analista_note",
                "coordenador_note",
                "termo_antecipacao_path",
                "termo_antecipacao_original_name",
                "termo_antecipacao_mime",
                "termo_antecipacao_size_bytes",
                "termo_antecipacao_uploaded_at",
                "observacao",
                "updated_at",
            ]
        )

        if termo_antecipacao is not None:
            Comprovante.objects.create(
                refinanciamento=refinanciamento,
                contrato=contrato,
                ciclo=refinanciamento.ciclo_origem,
                tipo=Comprovante.Tipo.TERMO_ANTECIPACAO,
                papel=Comprovante.Papel.AGENTE,
                origem=Comprovante.Origem.SOLICITACAO_RENOVACAO,
                arquivo=termo_antecipacao,
                nome_original=getattr(termo_antecipacao, "name", ""),
                enviado_por=user,
            )

        assumption = RefinanciamentoService._assumption_for_refinanciamento(refinanciamento)
        if assumption:
            assumption.status = Assumption.Status.LIBERADO
            assumption.solicitado_por = user
            assumption.liberado_em = timezone.now()
            assumption.assumido_em = None
            assumption.finalizado_em = None
            assumption.analista = None
            assumption.heartbeat_at = None
            assumption.save(
                update_fields=[
                    "status",
                    "solicitado_por",
                    "liberado_em",
                    "assumido_em",
                    "finalizado_em",
                    "analista",
                    "heartbeat_at",
                    "updated_at",
                ]
            )

        RefinanciamentoService._registrar_auditoria(
            contrato,
            user,
            (
                "reenviar_termo_renovacao"
                if reenviado_pelo_agente
                else "solicitar_renovacao_analise"
            ),
            refinanciamento.observacao,
        )
        return refinanciamento

    @staticmethod
    @transaction.atomic
    def solicitar_liquidacao(contrato_id: int, user) -> Refinanciamento:
        contrato = RefinanciamentoService._get_contrato(contrato_id)
        evaluation = RefinanciamentoService.strategy.evaluate(contrato)
        if not evaluation["elegivel"]:
            raise ValidationError(evaluation["motivo"])

        _, refinanciamento = RefinanciamentoService._sync_contract_and_get_refi(
            contrato
        )
        if refinanciamento is None:
            # Fallback: a strategy ja aprovou elegibilidade, mas o rebuild pode
            # ter sido bloqueado por um refi anterior em REVERTIDO/DESATIVADO no
            # mesmo ciclo (has_cancelled_operational_refinanciamento). Forca a
            # materializacao porque o usuario esta explicitamente solicitando.
            rebuild_contract_cycle_state(
                contrato,
                execute=True,
                force_active_operational_status=Refinanciamento.Status.APTO_A_RENOVAR,
            )
            contrato.refresh_from_db()
            refinanciamento = RefinanciamentoService._active_operational_refinanciamento(
                contrato
            )
        if refinanciamento is None:
            raise ValidationError(
                "A fila operacional de renovação não pôde ser materializada."
            )

        normalized_status = _normalize_status(refinanciamento.status)
        if normalized_status != Refinanciamento.Status.APTO_A_RENOVAR:
            raise ValidationError(
                "Este contrato já foi encaminhado para renovação ou liquidação."
            )

        refinanciamento.status = Refinanciamento.Status.SOLICITADO_PARA_LIQUIDACAO
        refinanciamento.solicitado_por = user
        refinanciamento.reviewed_by = None
        refinanciamento.reviewed_at = None
        refinanciamento.analista_note = ""
        refinanciamento.coordenador_note = ""
        refinanciamento.observacao = "Solicitação de liquidação aberta pelo agente."
        refinanciamento.save(
            update_fields=[
                "status",
                "solicitado_por",
                "reviewed_by",
                "reviewed_at",
                "analista_note",
                "coordenador_note",
                "observacao",
                "updated_at",
            ]
        )

        RefinanciamentoService._registrar_auditoria(
            contrato,
            user,
            "solicitar_liquidacao_renovacao",
            "Agente optou por encerrar o contrato via liquidação em vez de enviar a renovação.",
        )
        return refinanciamento

    @staticmethod
    @transaction.atomic
    def aprovar(refinanciamento_id: int, user, observacao: str = "") -> Refinanciamento:
        refinanciamento = RefinanciamentoService._get_refinanciamento(refinanciamento_id)
        normalized = _normalize_status(refinanciamento.status)

        # Fluxo legado sem termo: agente não submeteu termo e a coordenação aprova diretamente.
        is_legado_sem_termo = (
            refinanciamento.status == Refinanciamento.Status.EM_ANALISE_RENOVACAO
            and not refinanciamento.termo_antecipacao_path
        )

        if not is_legado_sem_termo and normalized != Refinanciamento.Status.APROVADO_ANALISE_RENOVACAO:
            raise ValidationError(
                "Somente renovações aprovadas pela análise podem seguir para a tesouraria."
            )

        if is_legado_sem_termo:
            # Materializa o próximo ciclo via rebuild e vincula como ciclo_destino.
            contrato = refinanciamento.contrato_origem
            rebuild_contract_cycle_state(contrato, execute=True)
            refinanciamento.refresh_from_db()
            ciclo_origem_numero = (
                refinanciamento.ciclo_origem.numero if refinanciamento.ciclo_origem_id else 1
            )
            ciclo_destino = (
                Ciclo.objects.filter(
                    contrato=contrato,
                    numero=ciclo_origem_numero + 1,
                    deleted_at__isnull=True,
                ).first()
            )
            refinanciamento.status = Refinanciamento.Status.CONCLUIDO
            refinanciamento.aprovado_por = user
            refinanciamento.coordenador_note = observacao.strip() or "Renovação validada pela coordenação."
            refinanciamento.ciclo_destino = ciclo_destino
            refinanciamento.save(
                update_fields=["status", "aprovado_por", "coordenador_note", "ciclo_destino", "updated_at"]
            )
        else:
            refinanciamento.status = Refinanciamento.Status.APROVADO_PARA_RENOVACAO
            refinanciamento.aprovado_por = user
            refinanciamento.coordenador_note = (
                observacao.strip() or "Renovação validada pela coordenação."
            )
            refinanciamento.save(
                update_fields=["status", "aprovado_por", "coordenador_note", "updated_at"]
            )

        observacao_auditoria = (
            "Coordenação validou os anexos e encaminhou a renovação para a tesouraria."
        )
        if not RefinanciamentoService._mover_esteira_para_tesouraria(
            refinanciamento.contrato_origem,
            user,
            acao="aprovar_renovacao_coordenacao",
            observacao=observacao_auditoria,
        ):
            RefinanciamentoService._registrar_auditoria(
                refinanciamento.contrato_origem,
                user,
                "aprovar_renovacao_coordenacao",
                observacao_auditoria,
            )
        return refinanciamento

    @staticmethod
    @transaction.atomic
    def encaminhar_para_liquidacao(
        refinanciamento_id: int,
        user,
        observacao: str = "",
    ) -> Refinanciamento:
        refinanciamento = RefinanciamentoService._get_refinanciamento(refinanciamento_id)
        if _normalize_status(refinanciamento.status) not in {
            Refinanciamento.Status.APROVADO_ANALISE_RENOVACAO,
            Refinanciamento.Status.APROVADO_PARA_RENOVACAO,
        }:
            raise ValidationError(
                "Somente renovações em validação ou já encaminhadas podem seguir para liquidação."
            )

        mensagem = (
            observacao.strip()
            or "Renovação cancelada pela coordenação e enviada para a fila de liquidação."
        )
        refinanciamento.status = Refinanciamento.Status.SOLICITADO_PARA_LIQUIDACAO
        refinanciamento.coordenador_note = mensagem
        refinanciamento.observacao = mensagem
        refinanciamento.save(
            update_fields=[
                "status",
                "coordenador_note",
                "observacao",
                "updated_at",
            ]
        )

        if not RefinanciamentoService._mover_esteira_para_tesouraria(
            refinanciamento.contrato_origem,
            user,
            acao="encaminhar_liquidacao_renovacao",
            observacao=mensagem,
        ):
            RefinanciamentoService._registrar_auditoria(
                refinanciamento.contrato_origem,
                user,
                "encaminhar_liquidacao_renovacao",
                mensagem,
            )
        return refinanciamento

    @staticmethod
    @transaction.atomic
    def assumir_analise(refinanciamento_id: int, user) -> Refinanciamento:
        refinanciamento = RefinanciamentoService._get_refinanciamento(refinanciamento_id)
        if refinanciamento.status not in {
            Refinanciamento.Status.EM_ANALISE_RENOVACAO,
            Refinanciamento.Status.PENDENTE_TERMO_ANALISTA,
        }:
            raise ValidationError("A renovação não está disponível para análise.")

        assumption = RefinanciamentoService._assumption_for_refinanciamento(refinanciamento)
        if assumption is None:
            raise ValidationError("Nenhuma fila analítica foi encontrada para esta renovação.")
        if (
            assumption.status == Assumption.Status.ASSUMIDO
            and assumption.analista_id
            and assumption.analista_id != user.id
        ):
            raise ValidationError("Esta renovação já foi assumida por outro analista.")

        assumption.analista = user
        assumption.status = Assumption.Status.ASSUMIDO
        assumption.assumido_em = timezone.now()
        assumption.heartbeat_at = timezone.now()
        assumption.save(
            update_fields=["analista", "status", "assumido_em", "heartbeat_at", "updated_at"]
        )

        RefinanciamentoService._registrar_auditoria(
            refinanciamento.contrato_origem,
            user,
            "assumir_renovacao_analise",
            "Renovação assumida na fila analítica.",
        )
        return refinanciamento

    @staticmethod
    @transaction.atomic
    def aprovar_analise(
        refinanciamento_id: int,
        user,
        observacao: str = "",
    ) -> Refinanciamento:
        refinanciamento = RefinanciamentoService._get_refinanciamento(refinanciamento_id)
        if refinanciamento.status not in {
            Refinanciamento.Status.EM_ANALISE_RENOVACAO,
            Refinanciamento.Status.PENDENTE_TERMO_ANALISTA,
        }:
            raise ValidationError("A renovação não está disponível para aprovação analítica.")

        assumption = RefinanciamentoService._assumption_for_refinanciamento(refinanciamento)
        if assumption and assumption.analista_id and assumption.analista_id != user.id:
            raise ValidationError("A renovação foi assumida por outro analista.")

        refinanciamento.status = Refinanciamento.Status.APROVADO_ANALISE_RENOVACAO
        refinanciamento.reviewed_by = user
        refinanciamento.reviewed_at = timezone.now()
        refinanciamento.analista_note = observacao
        refinanciamento.save(
            update_fields=[
                "status",
                "reviewed_by",
                "reviewed_at",
                "analista_note",
                "updated_at",
            ]
        )

        if assumption:
            if assumption.analista_id is None:
                assumption.analista = user
            assumption.status = Assumption.Status.FINALIZADO
            assumption.finalizado_em = timezone.now()
            assumption.save(
                update_fields=["analista", "status", "finalizado_em", "updated_at"]
            )

        RefinanciamentoService._registrar_auditoria(
            refinanciamento.contrato_origem,
            user,
            "aprovar_renovacao_analise",
            "Analista aprovou a renovação e encaminhou o caso para validação da coordenação.",
        )
        return refinanciamento

    @staticmethod
    @transaction.atomic
    def devolver_para_analise(
        refinanciamento_id: int,
        user,
        observacao: str,
    ) -> Refinanciamento:
        refinanciamento = RefinanciamentoService._get_refinanciamento(refinanciamento_id)
        if _normalize_status(refinanciamento.status) != (
            Refinanciamento.Status.APROVADO_ANALISE_RENOVACAO
        ):
            raise ValidationError(
                "Somente renovações aprovadas pela análise podem ser devolvidas ao analista."
            )

        mensagem = observacao.strip()
        if not mensagem:
            raise ValidationError("Informe o motivo da devolução para a análise.")

        refinanciamento.status = Refinanciamento.Status.PENDENTE_TERMO_ANALISTA
        refinanciamento.coordenador_note = mensagem
        refinanciamento.observacao = mensagem
        refinanciamento.save(
            update_fields=[
                "status",
                "coordenador_note",
                "observacao",
                "updated_at",
            ]
        )
        RefinanciamentoService._reabrir_assumption_para_analise(refinanciamento)
        RefinanciamentoService._registrar_auditoria(
            refinanciamento.contrato_origem,
            user,
            "devolver_renovacao_para_analise",
            mensagem,
        )
        return refinanciamento

    @staticmethod
    @transaction.atomic
    def devolver_para_agente(
        refinanciamento_id: int,
        user,
        observacao: str,
    ) -> Refinanciamento:
        refinanciamento = RefinanciamentoService._get_refinanciamento(refinanciamento_id)
        if refinanciamento.status not in {
            Refinanciamento.Status.EM_ANALISE_RENOVACAO,
            Refinanciamento.Status.PENDENTE_TERMO_ANALISTA,
        }:
            raise ValidationError(
                "Somente renovações em análise podem ser devolvidas ao agente."
            )

        mensagem = observacao.strip()
        if not mensagem:
            raise ValidationError("Informe o motivo da devolução para o agente.")

        assumption = RefinanciamentoService._assumption_for_refinanciamento(refinanciamento)
        refinanciamento.status = Refinanciamento.Status.PENDENTE_TERMO_AGENTE
        refinanciamento.reviewed_by = user
        refinanciamento.reviewed_at = timezone.now()
        refinanciamento.analista_note = mensagem
        refinanciamento.observacao = mensagem
        refinanciamento.save(
            update_fields=[
                "status",
                "reviewed_by",
                "reviewed_at",
                "analista_note",
                "observacao",
                "updated_at",
            ]
        )

        if assumption:
            assumption.status = Assumption.Status.FINALIZADO
            if assumption.analista_id is None:
                assumption.analista = user
            assumption.finalizado_em = timezone.now()
            assumption.save(
                update_fields=[
                    "status",
                    "analista",
                    "finalizado_em",
                    "updated_at",
                ]
            )

        RefinanciamentoService._registrar_auditoria(
            refinanciamento.contrato_origem,
            user,
            "devolver_renovacao_para_agente",
            mensagem,
        )
        return refinanciamento

    @staticmethod
    @transaction.atomic
    def bloquear(refinanciamento_id: int, motivo: str, user) -> Refinanciamento:
        refinanciamento = RefinanciamentoService._get_refinanciamento(refinanciamento_id)
        if refinanciamento.status in [
            Refinanciamento.Status.BLOQUEADO,
            Refinanciamento.Status.REVERTIDO,
            Refinanciamento.Status.EFETIVADO,
            Refinanciamento.Status.DESATIVADO,
        ]:
            raise ValidationError("Refinanciamento já está em estado final.")

        refinanciamento.status = Refinanciamento.Status.BLOQUEADO
        refinanciamento.bloqueado_por = user
        refinanciamento.motivo_bloqueio = motivo
        refinanciamento.observacao = motivo
        refinanciamento.save(
            update_fields=[
                "status",
                "bloqueado_por",
                "motivo_bloqueio",
                "observacao",
                "updated_at",
            ]
        )

        assumption = RefinanciamentoService._assumption_for_refinanciamento(refinanciamento)
        if assumption:
            assumption.status = Assumption.Status.FINALIZADO
            assumption.finalizado_em = timezone.now()
            assumption.save(update_fields=["status", "finalizado_em", "updated_at"])

        RefinanciamentoService._registrar_auditoria(
            refinanciamento.contrato_origem,
            user,
            "bloquear_refinanciamento",
            motivo,
        )
        return refinanciamento

    @staticmethod
    @transaction.atomic
    def retornar_para_pendente_pagamento(
        refinanciamento_id: int,
        user,
    ) -> Refinanciamento:
        refinanciamento = RefinanciamentoService._get_refinanciamento(refinanciamento_id)
        if refinanciamento.status == Refinanciamento.Status.APROVADO_PARA_RENOVACAO:
            return refinanciamento

        allowed_statuses = {
            Refinanciamento.Status.EFETIVADO,
            Refinanciamento.Status.BLOQUEADO,
            Refinanciamento.Status.REVERTIDO,
            Refinanciamento.Status.DESATIVADO,
        }
        if refinanciamento.status not in allowed_statuses:
            raise ValidationError(
                "Somente renovações efetivadas ou canceladas podem voltar para pendente de pagamento."
            )

        refinanciamento.status = Refinanciamento.Status.APROVADO_PARA_RENOVACAO
        refinanciamento.bloqueado_por = None
        refinanciamento.efetivado_por = None
        refinanciamento.motivo_bloqueio = ""
        refinanciamento.observacao = "Renovação devolvida para pendente de pagamento."
        refinanciamento.executado_em = None
        refinanciamento.data_ativacao_ciclo = None
        refinanciamento.ciclo_destino = None
        refinanciamento.save(
            update_fields=[
                "status",
                "bloqueado_por",
                "efetivado_por",
                "motivo_bloqueio",
                "observacao",
                "executado_em",
                "data_ativacao_ciclo",
                "ciclo_destino",
                "updated_at",
            ]
        )

        comprovantes = refinanciamento.comprovantes.filter(
            tipo__in=[
                Comprovante.Tipo.COMPROVANTE_PAGAMENTO_ASSOCIADO,
                Comprovante.Tipo.COMPROVANTE_PAGAMENTO_AGENTE,
            ],
            deleted_at__isnull=True,
        )
        for comprovante in comprovantes:
            if comprovante.data_pagamento is not None:
                comprovante.data_pagamento = None
                comprovante.save(update_fields=["data_pagamento", "updated_at"])

        pagamento = RefinanciamentoService._get_renewal_payment(refinanciamento)
        if pagamento is not None:
            pagamento.status = Pagamento.Status.PENDENTE
            pagamento.paid_at = None
            pagamento.notes = (
                f"Renovação retornada para pendente de pagamento via tesouraria para refi #{refinanciamento.id}."
            )
            pagamento = RefinanciamentoService._sync_renewal_payment_paths(
                pagamento,
                refinanciamento,
            )
            pagamento.save(
                update_fields=[
                    "status",
                    "paid_at",
                    "notes",
                    "comprovante_associado_path",
                    "comprovante_agente_path",
                    "updated_at",
                ]
            )

        contrato = refinanciamento.contrato_origem
        if contrato is not None:
            rebuild_contract_cycle_state(contrato, execute=True)
            sync_associado_mother_status(contrato.associado)
            refinanciamento.refresh_from_db()

        if contrato is not None:
            RefinanciamentoService._registrar_auditoria(
                contrato,
                user,
                "retornar_para_pendente_pagamento",
                "Renovação devolvida para pendente de pagamento na fila da tesouraria.",
            )
        return refinanciamento

    @staticmethod
    @transaction.atomic
    def limpar_linha_operacional(
        refinanciamento_id: int,
        *,
        motivo: str,
        user,
    ) -> None:
        refinanciamento = RefinanciamentoService._get_refinanciamento(refinanciamento_id)
        refinanciamento.observacao = motivo or "Linha operacional removida manualmente."
        refinanciamento.save(update_fields=["observacao", "updated_at"])
        refinanciamento.soft_delete()
        RefinanciamentoService._registrar_auditoria(
            refinanciamento.contrato_origem,
            user,
            "limpar_linha_operacional_refinanciamento",
            refinanciamento.observacao,
        )

    @staticmethod
    @transaction.atomic
    def desativar(refinanciamento_id: int, motivo: str, user) -> Refinanciamento:
        raise ValidationError(
            "A desativação direta da renovação foi bloqueada. Use a liquidação do contrato para encerrar os meses pendentes."
        )

    @staticmethod
    @transaction.atomic
    def reverter(refinanciamento_id: int, user) -> Refinanciamento:
        refinanciamento = RefinanciamentoService._get_refinanciamento(refinanciamento_id)
        if refinanciamento.status not in [
            Refinanciamento.Status.APROVADO_ANALISE_RENOVACAO,
            Refinanciamento.Status.APROVADO_PARA_RENOVACAO,
            Refinanciamento.Status.CONCLUIDO,
        ]:
            raise ValidationError("Somente renovações aprovadas podem ser revertidas.")

        refinanciamento.status = Refinanciamento.Status.REVERTIDO
        refinanciamento.observacao = "Renovação revertida."
        refinanciamento.save(update_fields=["status", "observacao", "updated_at"])

        RefinanciamentoService._registrar_auditoria(
            refinanciamento.contrato_origem,
            user,
            "reverter_refinanciamento",
            "Renovação revertida antes da efetivação da tesouraria.",
        )
        return refinanciamento

    @staticmethod
    @transaction.atomic
    def efetivar(
        refinanciamento_id: int,
        comprovante_associado,
        comprovante_agente,
        user,
        *,
        proximo_ciclo_parcelas: int | None = None,
    ) -> Refinanciamento:
        refinanciamento = RefinanciamentoService._get_refinanciamento(refinanciamento_id)
        if (
            refinanciamento.status != Refinanciamento.Status.CONCLUIDO
            and _normalize_status(refinanciamento.status) != Refinanciamento.Status.APROVADO_PARA_RENOVACAO
        ):
            raise ValidationError(
                "Somente renovações validadas pela coordenação podem ser efetivadas."
            )

        contrato = refinanciamento.contrato_origem
        if contrato is None:
            raise ValidationError("Renovação sem contrato de origem.")

        if proximo_ciclo_parcelas is not None and int(proximo_ciclo_parcelas) not in (3, 4):
            raise ValidationError(
                "O próximo ciclo deve ter 3 ou 4 parcelas."
            )

        executado_em = timezone.now()
        if comprovante_associado:
            RefinanciamentoService._upsert_payment_comprovante(
                refinanciamento,
                papel=Comprovante.Papel.ASSOCIADO,
                arquivo=comprovante_associado,
                user=user,
                data_pagamento=executado_em,
            )
        if comprovante_agente:
            RefinanciamentoService._upsert_payment_comprovante(
                refinanciamento,
                papel=Comprovante.Papel.AGENTE,
                arquivo=comprovante_agente,
                user=user,
                data_pagamento=executado_em,
            )
        comprovante_associado_db, comprovante_agente_db = (
            RefinanciamentoService._require_effectivation_comprovantes(refinanciamento)
        )
        for comprovante in (comprovante_associado_db, comprovante_agente_db):
            if comprovante.data_pagamento != executado_em:
                comprovante.data_pagamento = executado_em
                comprovante.save(update_fields=["data_pagamento", "updated_at"])

        refinanciamento.status = Refinanciamento.Status.EFETIVADO
        refinanciamento.efetivado_por = user
        refinanciamento.executado_em = executado_em
        refinanciamento.data_ativacao_ciclo = executado_em
        refinanciamento.origem = Refinanciamento.Origem.OPERACIONAL
        refinanciamento.observacao = "Renovação efetivada pela tesouraria."
        refinanciamento.save(
            update_fields=[
                "status",
                "efetivado_por",
                "executado_em",
                "data_ativacao_ciclo",
                "origem",
                "observacao",
                "updated_at",
            ]
        )

        rebuild_contract_cycle_state(contrato, execute=True)
        refinanciamento.refresh_from_db()
        if refinanciamento.ciclo_destino_id is None and refinanciamento.ciclo_origem_id is not None:
            refinanciamento.ciclo_destino = (
                Ciclo.objects.filter(
                    contrato=contrato,
                    numero=refinanciamento.ciclo_origem.numero + 1,
                    deleted_at__isnull=True,
                )
                .order_by("id")
                .first()
            )
            if refinanciamento.ciclo_destino_id is not None:
                refinanciamento.save(update_fields=["ciclo_destino", "updated_at"])

        if refinanciamento.ciclo_origem_id is not None:
            ciclo_origem = refinanciamento.ciclo_origem
            cycle_size = (
                int(proximo_ciclo_parcelas)
                if proximo_ciclo_parcelas is not None
                else get_contract_cycle_size(contrato)
            )
            data_inicio = get_destination_cycle_start(ciclo_origem)

            if refinanciamento.ciclo_destino_id is None:
                data_fim = add_months(data_inicio, cycle_size - 1)
                destino_numero = ciclo_origem.numero + 1
                # UNIQUE(contrato, numero) inclui linhas soft-deleted no MySQL.
                # Se houve uma tentativa anterior que foi revertida e deixou um
                # ciclo soft-deleted com este numero, reutilizamos restaurando.
                ciclo_existente = (
                    Ciclo.all_objects.filter(
                        contrato=contrato,
                        numero=destino_numero,
                    )
                    .order_by("-id")
                    .first()
                )
                if ciclo_existente is not None:
                    novo_ciclo = ciclo_existente
                    if novo_ciclo.deleted_at is not None:
                        novo_ciclo.deleted_at = None
                    novo_ciclo.status = Ciclo.Status.ABERTO
                    novo_ciclo.data_inicio = data_inicio
                    novo_ciclo.data_fim = data_fim
                    novo_ciclo.valor_total = (
                        contrato.valor_mensalidade or Decimal("0")
                    ) * cycle_size
                    novo_ciclo.save(
                        update_fields=[
                            "deleted_at",
                            "status",
                            "data_inicio",
                            "data_fim",
                            "valor_total",
                            "updated_at",
                        ]
                    )
                else:
                    novo_ciclo = Ciclo.objects.create(
                        contrato=contrato,
                        numero=destino_numero,
                        data_inicio=data_inicio,
                        data_fim=data_fim,
                        status=Ciclo.Status.ABERTO,
                        valor_total=(contrato.valor_mensalidade or Decimal("0")) * cycle_size,
                    )
                refinanciamento.ciclo_destino = novo_ciclo
                refinanciamento.save(update_fields=["ciclo_destino", "updated_at"])
            else:
                novo_ciclo = refinanciamento.ciclo_destino

            # Parcelas vivas do ciclo (ja existentes apos restauracao/criacao).
            live_parcelas = list(
                novo_ciclo.parcelas.filter(deleted_at__isnull=True).order_by("numero")
            )
            existing_refs_to_live = {p.referencia_mes: p for p in live_parcelas}
            used_numeros = {p.numero for p in live_parcelas}
            if len(live_parcelas) < cycle_size:
                # Index para reutilizar parcelas soft-deleted do mesmo ciclo
                # antes de criar novas (evita conflito com UNIQUE(ciclo, numero)).
                soft_deleted_pool = list(
                    Parcela.all_objects.filter(
                        ciclo=novo_ciclo,
                        deleted_at__isnull=False,
                    ).order_by("numero")
                )
                next_numero = (max(used_numeros) if used_numeros else 0) + 1
                for index in range(cycle_size):
                    referencia = add_months(data_inicio, index)
                    if referencia in existing_refs_to_live:
                        continue
                    while next_numero in used_numeros:
                        next_numero += 1
                    # Reutiliza um soft-deleted (preferindo mesmo numero) ao
                    # inves de inserir nova linha
                    reused = next(
                        (p for p in soft_deleted_pool if p.numero == next_numero),
                        None,
                    ) or (soft_deleted_pool.pop(0) if soft_deleted_pool else None)
                    if reused is not None:
                        soft_deleted_pool = [p for p in soft_deleted_pool if p.pk != reused.pk]
                        reused.deleted_at = None
                        reused.numero = next_numero
                        reused.referencia_mes = referencia
                        reused.valor = contrato.valor_mensalidade or Decimal("0")
                        reused.data_vencimento = referencia
                        reused.data_pagamento = None
                        reused.status = Parcela.Status.EM_PREVISAO
                        reused.layout_bucket = Parcela.LayoutBucket.CYCLE
                        reused.associado = contrato.associado
                        reused.save(
                            update_fields=[
                                "deleted_at",
                                "numero",
                                "referencia_mes",
                                "valor",
                                "data_vencimento",
                                "data_pagamento",
                                "status",
                                "layout_bucket",
                                "associado",
                                "updated_at",
                            ]
                        )
                    else:
                        Parcela.objects.create(
                            ciclo=novo_ciclo,
                            associado=contrato.associado,
                            numero=next_numero,
                            referencia_mes=referencia,
                            valor=contrato.valor_mensalidade or Decimal("0"),
                            data_vencimento=referencia,
                            status=Parcela.Status.EM_PREVISAO,
                        )
                    used_numeros.add(next_numero)
                    next_numero += 1
                expected_total = (contrato.valor_mensalidade or Decimal("0")) * cycle_size
                if novo_ciclo.valor_total != expected_total:
                    novo_ciclo.valor_total = expected_total
                    novo_ciclo.save(update_fields=["valor_total", "updated_at"])

            if (
                ciclo_origem.status
                not in (Ciclo.Status.CICLO_RENOVADO, "ciclo_renovado")
                and not any(
                    parcela.status
                    not in (
                        Parcela.Status.DESCONTADO,
                        Parcela.Status.LIQUIDADA,
                        "quitada",
                    )
                    for parcela in ciclo_origem.parcelas.filter(deleted_at__isnull=True)
                )
            ):
                ciclo_origem.status = Ciclo.Status.CICLO_RENOVADO
                ciclo_origem.save(update_fields=["status", "updated_at"])

        if (
            proximo_ciclo_parcelas is not None
            and int(contrato.prazo_meses or 0) != int(proximo_ciclo_parcelas)
        ):
            old_prazo_efetivar = int(contrato.prazo_meses or 0)
            contrato.prazo_meses = int(proximo_ciclo_parcelas)
            update_fields = ["prazo_meses", "updated_at"]
            # Recalcula comissao proporcionalmente: 3->4 *= 4/3, 4->3 *= 3/4.
            if old_prazo_efetivar > 0 and contrato.comissao_agente:
                contrato.comissao_agente = (
                    (
                        Decimal(contrato.comissao_agente)
                        * Decimal(int(proximo_ciclo_parcelas))
                    )
                    / Decimal(old_prazo_efetivar)
                ).quantize(Decimal("0.01"))
                update_fields.append("comissao_agente")
            contrato.save(update_fields=update_fields)
            # Atualiza tambem o repasse do refinanciamento para refletir
            # imediatamente na tela da tesouraria.
            if "comissao_agente" in update_fields:
                refinanciamento.repasse_agente = contrato.comissao_agente
                refinanciamento.save(
                    update_fields=["repasse_agente", "updated_at"]
                )

        for papel in [Comprovante.Papel.ASSOCIADO, Comprovante.Papel.AGENTE]:
            comprovante = RefinanciamentoService._latest_payment_comprovante(
                refinanciamento,
                papel,
            )
            if comprovante is None:
                continue
            comprovante.ciclo = refinanciamento.ciclo_destino
            comprovante.save(update_fields=["ciclo", "updated_at"])

        RefinanciamentoService._upsert_renewal_payment(
            refinanciamento,
            user=user,
            paid_at=executado_em,
        )
        associado = contrato.associado
        if associado.status in {
            Associado.Status.INADIMPLENTE,
            Associado.Status.APTO_A_RENOVAR,
        }:
            associado.status = Associado.Status.ATIVO
            associado.save(update_fields=["status", "updated_at"])
        sync_associado_mother_status(associado)

        RefinanciamentoService._registrar_auditoria(
            contrato,
            user,
            "efetivar_refinanciamento",
            "Tesouraria anexou comprovantes e materializou o próximo ciclo.",
        )
        return refinanciamento

    @staticmethod
    @transaction.atomic
    def substituir_comprovante(
        refinanciamento_id: int,
        *,
        papel: str,
        arquivo,
        user,
    ) -> Refinanciamento:
        refinanciamento = RefinanciamentoService._get_refinanciamento(refinanciamento_id)
        now = timezone.now()
        RefinanciamentoService._upsert_payment_comprovante(
            refinanciamento,
            papel=papel,
            arquivo=arquivo,
            user=user,
            data_pagamento=now,
        )
        pagamento = RefinanciamentoService._get_renewal_payment(refinanciamento)
        if pagamento is not None:
            RefinanciamentoService._sync_renewal_payment_paths(
                pagamento,
                refinanciamento,
            )
            pagamento.save(
                update_fields=[
                    "comprovante_associado_path",
                    "comprovante_agente_path",
                    "updated_at",
                ]
            )

        refinanciamento.refresh_from_db()
        return refinanciamento

    @staticmethod
    @transaction.atomic
    def substituir_termo_agente(
        refinanciamento_id: int,
        *,
        arquivo,
        user,
    ) -> Refinanciamento:
        refinanciamento = RefinanciamentoService._get_refinanciamento(refinanciamento_id)
        comprovante = RefinanciamentoService._upsert_termo_comprovante(
            refinanciamento,
            arquivo=arquivo,
            user=user,
        )
        refinanciamento.termo_antecipacao_path = comprovante.arquivo_referencia
        refinanciamento.termo_antecipacao_original_name = comprovante.nome_original
        refinanciamento.termo_antecipacao_mime = comprovante.mime
        refinanciamento.termo_antecipacao_size_bytes = comprovante.size_bytes
        refinanciamento.termo_antecipacao_uploaded_at = timezone.now()
        refinanciamento.save(
            update_fields=[
                "termo_antecipacao_path",
                "termo_antecipacao_original_name",
                "termo_antecipacao_mime",
                "termo_antecipacao_size_bytes",
                "termo_antecipacao_uploaded_at",
                "updated_at",
            ]
        )
        refinanciamento.refresh_from_db()
        return refinanciamento
