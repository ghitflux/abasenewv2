from __future__ import annotations

from decimal import Decimal
import re

from django.db.models import Count, DateTimeField, OuterRef, Prefetch, Q, Subquery, Sum
from django.db.models.functions import Coalesce
from django.utils.dateparse import parse_date
from rest_framework import mixins, permissions, status
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet

from apps.accounts.permissions import (
    IsAgenteOrAdmin,
    IsAgenteOrAnalistaOrCoordenadorOrAdmin,
    IsAgenteOrCoordenadorOrAdmin,
    IsAnalistaOrAdmin,
    IsCoordenadorOrTesoureiroOrAdmin,
    IsCoordenadorOrAdmin,
    IsTesoureiroOrAdmin,
)
from core.pagination import StandardResultsSetPagination

from .models import Assumption, Comprovante, Refinanciamento
from .serializers import (
    AprovarAnaliseRefinanciamentoSerializer,
    AprovarRefinanciamentoSerializer,
    AprovarEmMassaRefinanciamentoSerializer,
    BloquearRefinanciamentoSerializer,
    DevolverRefinanciamentoSerializer,
    DesativarRefinanciamentoSerializer,
    EncaminharLiquidacaoRefinanciamentoSerializer,
    EfetivarRefinanciamentoSerializer,
    ElegibilidadeRefinanciamentoSerializer,
    RefinanciamentoDetailSerializer,
    RefinanciamentoListSerializer,
    SolicitarLiquidacaoRefinanciamentoSerializer,
    SolicitarRefinanciamentoSerializer,
    SubstituirComprovanteRefinanciamentoSerializer,
    SubstituirTermoAgenteRefinanciamentoSerializer,
)
from .services import RefinanciamentoService
from .services import annotate_renewal_materialization, renewal_materialized_q


def _ghost_effectivado_q() -> Q:
    return Q(
        status=Refinanciamento.Status.EFETIVADO,
        executado_em__isnull=True,
        data_ativacao_ciclo__isnull=True,
        ciclo_destino__isnull=True,
    )


class BaseRefinanciamentoViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    GenericViewSet,
):
    queryset = Refinanciamento.objects.none()
    pagination_class = StandardResultsSetPagination
    permission_classes = [permissions.IsAuthenticated]

    def get_serializer_class(self):
        if self.action == "retrieve":
            return RefinanciamentoDetailSerializer
        return RefinanciamentoListSerializer

    def _get_multi_param(self, name: str) -> list[str]:
        values = [item for item in self.request.query_params.getlist(name) if item]
        if len(values) == 1 and "," in values[0]:
            return [chunk.strip() for chunk in values[0].split(",") if chunk.strip()]
        return values

    def _should_apply_base_status_filter(self, status_filters: list[str]) -> bool:
        return True

    def _apply_year_filter(self, queryset, year: int, status_filters: list[str]):
        return queryset.filter(competencia_solicitada__year=year)

    def _effective_resumo_count(self, queryset) -> int:
        return queryset.filter(status=Refinanciamento.Status.EFETIVADO).count()

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Refinanciamento.objects.none()

        assumptions = Assumption.objects.filter(
            cadastro_id=OuterRef("associado_id"),
            request_key=OuterRef("cycle_key"),
            deleted_at__isnull=True,
        ).order_by("-created_at", "-id")
        queryset = (
            Refinanciamento.objects.select_related(
                "associado",
                "contrato_origem",
                "contrato_origem__agente",
                "solicitado_por",
                "aprovado_por",
                "bloqueado_por",
                "efetivado_por",
                "ciclo_origem",
                "ciclo_destino",
            )
            .prefetch_related(
                Prefetch("ciclo_destino__parcelas"),
                Prefetch("ciclo_origem__parcelas"),
                Prefetch("comprovantes__enviado_por"),
                Prefetch("itens__pagamento_mensalidade"),
            )
            .annotate(
                assumption_analista_id=Subquery(
                    assumptions.values("analista_id")[:1]
                ),
                assumption_status_value=Subquery(assumptions.values("status")[:1]),
            )
            .order_by("-created_at")
        )
        comprovantes = Comprovante.objects.filter(
            refinanciamento_id=OuterRef("pk"),
            deleted_at__isnull=True,
        )
        associado_comprovante = comprovantes.filter(
            papel=Comprovante.Papel.ASSOCIADO,
            tipo=Comprovante.Tipo.COMPROVANTE_PAGAMENTO_ASSOCIADO,
        ).order_by("-updated_at", "-created_at", "-id")
        agente_comprovante = comprovantes.filter(
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
        queryset = annotate_renewal_materialization(queryset)

        search = self.request.query_params.get("search")
        if search:
            queryset = queryset.filter(
                Q(associado__nome_completo__icontains=search)
                | Q(associado__cpf_cnpj__icontains=search)
                | Q(contrato_origem__codigo__icontains=search)
                | Q(contrato_origem__agente__first_name__icontains=search)
                | Q(contrato_origem__agente__last_name__icontains=search)
                | Q(agente_snapshot__icontains=search)
            )

        status_filters = self._get_multi_param("status")
        if status_filters and self._should_apply_base_status_filter(status_filters):
            queryset = queryset.filter(status__in=status_filters)

        cycle_key = (self.request.query_params.get("cycle_key") or "").strip()
        if cycle_key:
            cycle_tokens = [
                token.strip()
                for token in re.split(r"[|,]", cycle_key)
                if token.strip()
            ]
            for token in cycle_tokens:
                queryset = queryset.filter(cycle_key__icontains=token)

        numero_ciclos = (self.request.query_params.get("numero_ciclos") or "").strip()
        if numero_ciclos:
            if not numero_ciclos.isdigit():
                raise ValidationError(
                    {"numero_ciclos": ["Informe um número de ciclo válido."]}
                )
            queryset = queryset.filter(ciclo_origem__numero=int(numero_ciclos))

        origin_filters = self._get_multi_param("origem")
        if origin_filters:
            queryset = queryset.filter(origem__in=origin_filters)

        competencia_start = parse_date(
            self.request.query_params.get("competencia_start", "")
        )
        if competencia_start:
            queryset = queryset.filter(competencia_solicitada__gte=competencia_start)

        competencia_end = parse_date(
            self.request.query_params.get("competencia_end", "")
        )
        if competencia_end:
            queryset = queryset.filter(competencia_solicitada__lte=competencia_end)

        year = (self.request.query_params.get("year") or "").strip()
        if year:
            if not year.isdigit():
                raise ValidationError({"year": ["Informe um ano válido."]})
            queryset = self._apply_year_filter(queryset, int(year), status_filters)

        agent_filter = self.request.query_params.get("agent")
        if agent_filter:
            queryset = queryset.filter(
                Q(contrato_origem__agente__first_name__icontains=agent_filter)
                | Q(contrato_origem__agente__last_name__icontains=agent_filter)
                | Q(agente_snapshot__icontains=agent_filter)
            )

        pagamento_feito = (self.request.query_params.get("pagamento_feito") or "").strip().lower()
        if pagamento_feito in {"1", "true", "sim", "yes"}:
            queryset = queryset.filter(data_pagamento_associado__isnull=False)
        elif pagamento_feito in {"0", "false", "nao", "não", "no"}:
            queryset = queryset.filter(data_pagamento_associado__isnull=True)

        eligibility_band = self.request.query_params.get("eligibility_band")
        if eligibility_band == "2_3":
            queryset = queryset.filter(parcelas_ok=2, contrato_origem__prazo_meses=3)
        elif eligibility_band == "3_3":
            queryset = queryset.filter(parcelas_ok__gte=3, contrato_origem__prazo_meses=3)
        elif eligibility_band == "3_4":
            queryset = queryset.filter(parcelas_ok=3, contrato_origem__prazo_meses=4)
        elif eligibility_band == "4_4":
            queryset = queryset.filter(parcelas_ok__gte=4, contrato_origem__prazo_meses=4)

        user = self.request.user
        if user.has_role("ADMIN"):
            return queryset
        if user.has_role("AGENTE"):
            return queryset.filter(contrato_origem__agente=user)
        if user.has_role("COORDENADOR"):
            return queryset
        if user.has_role("ANALISTA"):
            return queryset.filter(
                status__in=[
                    Refinanciamento.Status.EM_ANALISE_RENOVACAO,
                    Refinanciamento.Status.PENDENTE_TERMO_ANALISTA,
                    Refinanciamento.Status.APROVADO_ANALISE_RENOVACAO,
                ]
            )
        if user.has_role("TESOUREIRO"):
            return queryset.filter(
                status__in=[
                    Refinanciamento.Status.APROVADO_PARA_RENOVACAO,
                    Refinanciamento.Status.EFETIVADO,
                    Refinanciamento.Status.BLOQUEADO,
                    Refinanciamento.Status.REVERTIDO,
                    Refinanciamento.Status.DESATIVADO,
                ]
            )
        return queryset.none()

    @action(detail=False, methods=["get"], url_path="resumo")
    def resumo(self, request):
        queryset = self.get_queryset()
        resumo = queryset.aggregate(
            total=Count("id"),
            solicitados_liquidacao=Count(
                "id",
                filter=Q(status=Refinanciamento.Status.SOLICITADO_PARA_LIQUIDACAO),
            ),
            em_analise=Count(
                "id",
                filter=Q(
                    status__in=[
                        Refinanciamento.Status.EM_ANALISE_RENOVACAO,
                        Refinanciamento.Status.PENDENTE_TERMO_ANALISTA,
                    ]
                ),
            ),
            assumidos=Count(
                "id", filter=Q(assumption_status_value=Assumption.Status.ASSUMIDO)
            ),
            aprovados=Count(
                "id",
                filter=Q(status=Refinanciamento.Status.APROVADO_ANALISE_RENOVACAO),
            ),
            concluidos=Count(
                "id",
                filter=Q(
                    status__in=[
                        Refinanciamento.Status.CONCLUIDO,
                        Refinanciamento.Status.EFETIVADO,
                    ]
                ),
            ),
            bloqueados=Count(
                "id", filter=Q(status=Refinanciamento.Status.BLOQUEADO)
            ),
            revertidos=Count(
                "id", filter=Q(status=Refinanciamento.Status.REVERTIDO)
            ),
            desativados=Count(
                "id", filter=Q(status=Refinanciamento.Status.DESATIVADO)
            ),
            em_fluxo=Count(
                "id",
                filter=Q(
                    status__in=[
                        Refinanciamento.Status.APTO_A_RENOVAR,
                        Refinanciamento.Status.SOLICITADO_PARA_LIQUIDACAO,
                        Refinanciamento.Status.PENDENTE_APTO,
                        Refinanciamento.Status.SOLICITADO,
                        Refinanciamento.Status.EM_ANALISE,
                        Refinanciamento.Status.APROVADO,
                        Refinanciamento.Status.EM_ANALISE_RENOVACAO,
                        Refinanciamento.Status.PENDENTE_TERMO_ANALISTA,
                        Refinanciamento.Status.PENDENTE_TERMO_AGENTE,
                        Refinanciamento.Status.APROVADO_ANALISE_RENOVACAO,
                        Refinanciamento.Status.APROVADO_PARA_RENOVACAO,
                    ]
                ),
            ),
            repasse_total=Sum("repasse_agente"),
        )
        resumo["efetivados"] = self._effective_resumo_count(queryset)
        resumo["desistentes"] = (
            int(resumo.get("solicitados_liquidacao") or 0)
            + int(resumo.get("bloqueados") or 0)
            + int(resumo.get("revertidos") or 0)
            + int(resumo.get("desativados") or 0)
        )
        resumo["com_anexo_agente"] = (
            queryset.filter(comprovantes__papel=Comprovante.Papel.AGENTE)
            .values("id")
            .distinct()
            .count()
        )
        resumo["repasse_total"] = resumo["repasse_total"] or Decimal("0.00")
        return Response(resumo)


class RefinanciamentoViewSet(BaseRefinanciamentoViewSet):
    def get_permissions(self):
        if self.action in {"solicitar", "eligibilidade"}:
            return [
                permissions.IsAuthenticated(),
                IsAgenteOrAnalistaOrCoordenadorOrAdmin(),
            ]
        if self.action == "solicitar_liquidacao":
            return [permissions.IsAuthenticated(), IsAgenteOrCoordenadorOrAdmin()]
        if self.action in [
            "aprovar",
            "bloquear",
            "reverter",
            "desativar",
            "encaminhar_liquidacao",
            "devolver_analise",
        ]:
            return [permissions.IsAuthenticated(), IsCoordenadorOrAdmin()]
        if self.action in ["assumir_analise", "aprovar_analise", "devolver_agente"]:
            return [permissions.IsAuthenticated(), IsAnalistaOrAdmin()]
        if self.action == "efetivar":
            return [permissions.IsAuthenticated(), IsTesoureiroOrAdmin()]
        return [permissions.IsAuthenticated()]

    @action(detail=True, methods=["get"])
    def elegibilidade(self, request, pk=None):
        payload = RefinanciamentoService.verificar_elegibilidade(int(pk))
        return Response(ElegibilidadeRefinanciamentoSerializer(payload).data)

    @action(detail=True, methods=["post"], parser_classes=[MultiPartParser, FormParser])
    def solicitar(self, request, pk=None):
        termo_antecipacao = request.FILES.get("termo_antecipacao")
        if termo_antecipacao is None and "termo_antecipacao" in request.data:
            payload = SolicitarRefinanciamentoSerializer(data=request.data)
            payload.is_valid(raise_exception=True)
            termo_antecipacao = payload.validated_data["termo_antecipacao"]
        refinanciamento = RefinanciamentoService.solicitar(
            int(pk),
            termo_antecipacao,
            request.user,
        )
        serializer = RefinanciamentoDetailSerializer(
            refinanciamento, context=self.get_serializer_context()
        )
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="solicitar-liquidacao")
    def solicitar_liquidacao(self, request, pk=None):
        payload = SolicitarLiquidacaoRefinanciamentoSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        refinanciamento = RefinanciamentoService.solicitar_liquidacao(
            int(pk),
            request.user,
        )
        serializer = RefinanciamentoDetailSerializer(
            refinanciamento, context=self.get_serializer_context()
        )
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def aprovar(self, request, pk=None):
        payload = AprovarRefinanciamentoSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        refinanciamento = RefinanciamentoService.aprovar(
            int(pk),
            request.user,
            payload.validated_data.get("observacao", ""),
        )
        serializer = RefinanciamentoDetailSerializer(
            refinanciamento, context=self.get_serializer_context()
        )
        return Response(serializer.data)

    @action(detail=True, methods=["post"], url_path="devolver-analise")
    def devolver_analise(self, request, pk=None):
        payload = DevolverRefinanciamentoSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        refinanciamento = RefinanciamentoService.devolver_para_analise(
            int(pk),
            request.user,
            payload.validated_data["observacao"],
        )
        serializer = RefinanciamentoDetailSerializer(
            refinanciamento, context=self.get_serializer_context()
        )
        return Response(serializer.data)

    @action(detail=True, methods=["post"], url_path="encaminhar-liquidacao")
    def encaminhar_liquidacao(self, request, pk=None):
        payload = EncaminharLiquidacaoRefinanciamentoSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        refinanciamento = RefinanciamentoService.encaminhar_para_liquidacao(
            int(pk),
            request.user,
            payload.validated_data.get("observacao", ""),
        )
        serializer = RefinanciamentoDetailSerializer(
            refinanciamento, context=self.get_serializer_context()
        )
        return Response(serializer.data)

    @action(detail=True, methods=["post"])
    def bloquear(self, request, pk=None):
        payload = BloquearRefinanciamentoSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        refinanciamento = RefinanciamentoService.bloquear(
            int(pk), payload.validated_data["motivo"], request.user
        )
        serializer = RefinanciamentoDetailSerializer(
            refinanciamento, context=self.get_serializer_context()
        )
        return Response(serializer.data)

    @action(detail=True, methods=["post"])
    def desativar(self, request, pk=None):
        payload = DesativarRefinanciamentoSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        refinanciamento = RefinanciamentoService.desativar(
            int(pk), payload.validated_data["motivo"], request.user
        )
        serializer = RefinanciamentoDetailSerializer(
            refinanciamento, context=self.get_serializer_context()
        )
        return Response(serializer.data)

    @action(detail=True, methods=["post"])
    def assumir_analise(self, request, pk=None):
        refinanciamento = RefinanciamentoService.assumir_analise(int(pk), request.user)
        serializer = RefinanciamentoDetailSerializer(
            refinanciamento, context=self.get_serializer_context()
        )
        return Response(serializer.data)

    @action(detail=True, methods=["post"], parser_classes=[FormParser, JSONParser, MultiPartParser])
    def aprovar_analise(self, request, pk=None):
        payload = AprovarAnaliseRefinanciamentoSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        refinanciamento = RefinanciamentoService.aprovar_analise(
            int(pk),
            request.user,
            payload.validated_data.get("observacao", ""),
        )
        serializer = RefinanciamentoDetailSerializer(
            refinanciamento, context=self.get_serializer_context()
        )
        return Response(serializer.data)

    @action(detail=True, methods=["post"], url_path="devolver-agente")
    def devolver_agente(self, request, pk=None):
        payload = DevolverRefinanciamentoSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        refinanciamento = RefinanciamentoService.devolver_para_agente(
            int(pk),
            request.user,
            payload.validated_data["observacao"],
        )
        serializer = RefinanciamentoDetailSerializer(
            refinanciamento, context=self.get_serializer_context()
        )
        return Response(serializer.data)

    @action(detail=True, methods=["post"])
    def reverter(self, request, pk=None):
        refinanciamento = RefinanciamentoService.reverter(int(pk), request.user)
        serializer = RefinanciamentoDetailSerializer(
            refinanciamento, context=self.get_serializer_context()
        )
        return Response(serializer.data)

    @action(
        detail=True,
        methods=["post"],
        parser_classes=[FormParser, JSONParser, MultiPartParser],
    )
    def efetivar(self, request, pk=None):
        payload = EfetivarRefinanciamentoSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        refinanciamento = RefinanciamentoService.efetivar(
            int(pk),
            payload.validated_data.get("comprovante_associado"),
            payload.validated_data.get("comprovante_agente"),
            request.user,
        )
        serializer = RefinanciamentoDetailSerializer(
            refinanciamento, context=self.get_serializer_context()
        )
        return Response(serializer.data)


class CoordenadorRefinanciadosViewSet(BaseRefinanciamentoViewSet):
    permission_classes = [permissions.IsAuthenticated, IsCoordenadorOrAdmin]

    def _apply_year_filter(self, queryset, year: int, status_filters: list[str]):
        if set(status_filters) == {Refinanciamento.Status.EFETIVADO}:
            queryset = queryset.annotate(
                effective_reference=Coalesce(
                    "executado_em",
                    "data_ativacao_ciclo",
                    "linked_payment_paid_at",
                    "data_pagamento_associado",
                    "data_pagamento_agente",
                    "updated_at",
                    output_field=DateTimeField(),
                )
            )
            return queryset.filter(effective_reference__date__year=year)
        return super()._apply_year_filter(queryset, year, status_filters)

    def get_queryset(self):
        queryset = super().get_queryset().filter(
            status__in=[
                Refinanciamento.Status.APROVADO_ANALISE_RENOVACAO,
                Refinanciamento.Status.APROVADO_PARA_RENOVACAO,
                Refinanciamento.Status.EFETIVADO,
                Refinanciamento.Status.SOLICITADO_PARA_LIQUIDACAO,
            ]
        )
        return queryset


class CoordenadorRefinanciamentoViewSet(BaseRefinanciamentoViewSet):
    permission_classes = [permissions.IsAuthenticated, IsCoordenadorOrAdmin]

    def get_queryset(self):
        queryset = super().get_queryset().filter(
            status__in=[
                Refinanciamento.Status.APROVADO_ANALISE_RENOVACAO,
                Refinanciamento.Status.PENDENTE_TERMO_ANALISTA,
            ]
        )
        year = self.request.query_params.get("year")
        if year and year.isdigit():
            queryset = queryset.filter(competencia_solicitada__year=int(year))
        return queryset

    @action(detail=False, methods=["post"], url_path="aprovar_em_massa")
    def aprovar_em_massa(self, request):
        payload = AprovarEmMassaRefinanciamentoSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        ids = payload.validated_data["ids"]

        allowed_ids = set(self.get_queryset().filter(id__in=ids).values_list("id", flat=True))
        results: list[dict[str, object]] = []
        success_count = 0

        for refinanciamento_id in ids:
            if refinanciamento_id not in allowed_ids:
                results.append(
                    {
                        "id": refinanciamento_id,
                        "status": "falha",
                        "motivo": "Refinanciamento não encontrado ou fora do escopo da coordenação.",
                    }
                )
                continue
            try:
                RefinanciamentoService.aprovar(
                    refinanciamento_id,
                    request.user,
                )
            except ValidationError as exc:
                detail = exc.detail
                if isinstance(detail, list) and detail:
                    message = str(detail[0])
                elif isinstance(detail, dict):
                    first_value = next(iter(detail.values()), "")
                    if isinstance(first_value, list) and first_value:
                        message = str(first_value[0])
                    else:
                        message = str(first_value)
                else:
                    message = str(detail)
                results.append(
                    {
                        "id": refinanciamento_id,
                        "status": "falha",
                        "motivo": message or "Falha ao aprovar refinanciamento.",
                    }
                )
                continue

            success_count += 1
            results.append({"id": refinanciamento_id, "status": "sucesso", "motivo": ""})

        return Response(
            {
                "success_count": success_count,
                "failure_count": len(results) - success_count,
                "results": results,
            }
        )


class AnalistaRefinanciamentoViewSet(BaseRefinanciamentoViewSet):
    permission_classes = [permissions.IsAuthenticated, IsAnalistaOrAdmin]

    # Statuses que representam renovações efetivamente enviadas para o analista.
    # apto_a_renovar / pendente_apto / solicitado = ainda não enviados → excluídos.
    ANALISTA_STATUSES = [
        Refinanciamento.Status.EM_ANALISE_RENOVACAO,
        Refinanciamento.Status.PENDENTE_TERMO_ANALISTA,
        Refinanciamento.Status.PENDENTE_TERMO_AGENTE,
        Refinanciamento.Status.APROVADO_ANALISE_RENOVACAO,
    ]

    def get_queryset(self):
        queryset = super().get_queryset().filter(
            status__in=self.ANALISTA_STATUSES,
            deleted_at__isnull=True,
        )
        assignment = self.request.query_params.get("assignment")
        if assignment == "minhas":
            queryset = queryset.filter(assumption_analista_id=self.request.user.id)
        elif assignment == "assumidas":
            queryset = queryset.filter(
                assumption_status_value=Assumption.Status.ASSUMIDO
            )
        elif assignment == "nao_assumidas":
            queryset = queryset.filter(assumption_analista_id__isnull=True)
        return queryset


class TesourariaRefinanciamentoViewSet(BaseRefinanciamentoViewSet):
    def _should_apply_base_status_filter(self, status_filters: list[str]) -> bool:
        return set(status_filters) != {Refinanciamento.Status.EFETIVADO}

    def _apply_year_filter(self, queryset, year: int, status_filters: list[str]):
        if set(status_filters) == {Refinanciamento.Status.EFETIVADO}:
            queryset = queryset.annotate(
                tesouraria_operational_date=Coalesce(
                    "executado_em",
                    "data_ativacao_ciclo",
                    "linked_payment_paid_at",
                    "data_pagamento_associado",
                    "data_pagamento_agente",
                    "updated_at",
                    output_field=DateTimeField(),
                )
            )
            return queryset.filter(tesouraria_operational_date__date__year=year)
        return super()._apply_year_filter(queryset, year, status_filters)

    def _effective_resumo_count(self, queryset) -> int:
        return queryset.filter(renewal_materialized_q()).exclude(
            status__in=[
                Refinanciamento.Status.BLOQUEADO,
                Refinanciamento.Status.REVERTIDO,
                Refinanciamento.Status.DESATIVADO,
            ]
        ).count()

    def get_permissions(self):
        if self.action in {"efetivar", "substituir_comprovante"}:
            return [permissions.IsAuthenticated(), IsTesoureiroOrAdmin()]
        if self.action == "substituir_termo_agente":
            return [permissions.IsAuthenticated(), IsCoordenadorOrTesoureiroOrAdmin()]
        if self.action == "retornar_pendente":
            return [permissions.IsAuthenticated(), IsCoordenadorOrTesoureiroOrAdmin()]
        return [permissions.IsAuthenticated(), IsCoordenadorOrTesoureiroOrAdmin()]

    def get_queryset(self):
        queryset = super().get_queryset().filter(
            status__in=[
                Refinanciamento.Status.APROVADO_PARA_RENOVACAO,
                Refinanciamento.Status.EFETIVADO,
                Refinanciamento.Status.BLOQUEADO,
                Refinanciamento.Status.REVERTIDO,
                Refinanciamento.Status.DESATIVADO,
            ]
        )
        queryset = queryset.exclude(_ghost_effectivado_q())
        status_filters = set(self._get_multi_param("status"))
        pending_statuses = {Refinanciamento.Status.APROVADO_PARA_RENOVACAO}
        efetivado_statuses = {Refinanciamento.Status.EFETIVADO}
        cancelado_statuses = {
            Refinanciamento.Status.BLOQUEADO,
            Refinanciamento.Status.REVERTIDO,
            Refinanciamento.Status.DESATIVADO,
        }

        date_start = self.request.query_params.get("data_inicio")
        date_end = self.request.query_params.get("data_fim")
        assoc_attach_start = self.request.query_params.get("data_anexo_associado_inicio")
        assoc_attach_end = self.request.query_params.get("data_anexo_associado_fim")
        agent_attach_start = self.request.query_params.get("data_anexo_agente_inicio")
        agent_attach_end = self.request.query_params.get("data_anexo_agente_fim")
        assoc_paid_start = self.request.query_params.get("data_pagamento_associado_inicio")
        assoc_paid_end = self.request.query_params.get("data_pagamento_associado_fim")
        agent_paid_start = self.request.query_params.get("data_pagamento_agente_inicio")
        agent_paid_end = self.request.query_params.get("data_pagamento_agente_fim")

        if status_filters == pending_statuses:
            if date_start:
                queryset = queryset.filter(updated_at__date__gte=date_start)
            if date_end:
                queryset = queryset.filter(updated_at__date__lte=date_end)
            return queryset.order_by("-updated_at", "-id")

        if status_filters == efetivado_statuses:
            queryset = queryset.filter(renewal_materialized_q()).exclude(
                status__in=[
                    Refinanciamento.Status.BLOQUEADO,
                    Refinanciamento.Status.REVERTIDO,
                    Refinanciamento.Status.DESATIVADO,
                ]
            ).annotate(
                tesouraria_operational_date=Coalesce(
                    "executado_em",
                    "data_ativacao_ciclo",
                    "linked_payment_paid_at",
                    "data_pagamento_associado",
                    "data_pagamento_agente",
                    "updated_at",
                    output_field=DateTimeField(),
                )
            )
            if date_start:
                queryset = queryset.filter(tesouraria_operational_date__date__gte=date_start)
            if date_end:
                queryset = queryset.filter(tesouraria_operational_date__date__lte=date_end)
            return queryset.order_by("-tesouraria_operational_date", "-id")

        if status_filters and status_filters.issubset(cancelado_statuses):
            if date_start:
                queryset = queryset.filter(updated_at__date__gte=date_start)
            if date_end:
                queryset = queryset.filter(updated_at__date__lte=date_end)
            return queryset.order_by("-updated_at", "-id")

        if date_start:
            queryset = queryset.filter(updated_at__date__gte=date_start)
        if date_end:
            queryset = queryset.filter(updated_at__date__lte=date_end)
        if assoc_attach_start:
            queryset = queryset.filter(data_anexo_associado__date__gte=assoc_attach_start)
        if assoc_attach_end:
            queryset = queryset.filter(data_anexo_associado__date__lte=assoc_attach_end)
        if agent_attach_start:
            queryset = queryset.filter(data_anexo_agente__date__gte=agent_attach_start)
        if agent_attach_end:
            queryset = queryset.filter(data_anexo_agente__date__lte=agent_attach_end)
        if assoc_paid_start:
            queryset = queryset.filter(data_pagamento_associado__date__gte=assoc_paid_start)
        if assoc_paid_end:
            queryset = queryset.filter(data_pagamento_associado__date__lte=assoc_paid_end)
        if agent_paid_start:
            queryset = queryset.filter(data_pagamento_agente__date__gte=agent_paid_start)
        if agent_paid_end:
            queryset = queryset.filter(data_pagamento_agente__date__lte=agent_paid_end)

        return queryset.order_by("-updated_at", "-id")

    @action(
        detail=True,
        methods=["post"],
        parser_classes=[FormParser, JSONParser, MultiPartParser],
    )
    def efetivar(self, request, pk=None):
        payload = EfetivarRefinanciamentoSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        refinanciamento = RefinanciamentoService.efetivar(
            int(pk),
            payload.validated_data.get("comprovante_associado"),
            payload.validated_data.get("comprovante_agente"),
            request.user,
            proximo_ciclo_parcelas=payload.validated_data.get("proximo_ciclo_parcelas"),
        )
        serializer = RefinanciamentoDetailSerializer(
            refinanciamento, context=self.get_serializer_context()
        )
        return Response(serializer.data)

    @action(
        detail=True,
        methods=["post"],
        permission_classes=[
            permissions.IsAuthenticated,
            IsCoordenadorOrTesoureiroOrAdmin,
        ],
    )
    def excluir(self, request, pk=None):
        motivo = (request.data.get("motivo") or "").strip() or "Removido da fila pela coordenação."
        RefinanciamentoService.limpar_linha_operacional(
            int(pk),
            motivo=motivo,
            user=request.user,
        )
        return Response({"detail": "Linha operacional removida."})

    @action(
        detail=True,
        methods=["post"],
        permission_classes=[
            permissions.IsAuthenticated,
            IsCoordenadorOrTesoureiroOrAdmin,
        ],
        url_path="retornar-pendente",
    )
    def retornar_pendente(self, request, pk=None):
        refinanciamento = RefinanciamentoService.retornar_para_pendente_pagamento(
            int(pk),
            request.user,
        )
        serializer = RefinanciamentoDetailSerializer(
            refinanciamento, context=self.get_serializer_context()
        )
        return Response(serializer.data)

    @action(
        detail=True,
        methods=["post"],
        permission_classes=[
            permissions.IsAuthenticated,
            IsCoordenadorOrTesoureiroOrAdmin,
        ],
        url_path="limpar-linha",
    )
    def limpar_linha(self, request, pk=None):
        motivo = (
            request.data.get("motivo") or ""
        ).strip() or "Linha operacional removida manualmente."
        RefinanciamentoService.limpar_linha_operacional(
            int(pk),
            motivo=motivo,
            user=request.user,
        )
        return Response({"detail": "Linha operacional removida."})

    @action(detail=True, methods=["post"], parser_classes=[MultiPartParser], url_path="substituir-comprovante")
    def substituir_comprovante(self, request, pk=None):
        payload = SubstituirComprovanteRefinanciamentoSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        refinanciamento = RefinanciamentoService.substituir_comprovante(
            int(pk),
            papel=payload.validated_data["papel"],
            arquivo=payload.validated_data["arquivo"],
            user=request.user,
        )
        serializer = RefinanciamentoDetailSerializer(
            refinanciamento, context=self.get_serializer_context()
        )
        return Response(serializer.data)

    @action(
        detail=True,
        methods=["post"],
        parser_classes=[MultiPartParser],
        url_path="substituir-termo-agente",
    )
    def substituir_termo_agente(self, request, pk=None):
        payload = SubstituirTermoAgenteRefinanciamentoSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        refinanciamento = RefinanciamentoService.substituir_termo_agente(
            int(pk),
            arquivo=payload.validated_data["arquivo"],
            user=request.user,
        )
        serializer = RefinanciamentoDetailSerializer(
            refinanciamento, context=self.get_serializer_context()
        )
        return Response(serializer.data)
