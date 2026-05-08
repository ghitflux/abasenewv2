from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from functools import lru_cache

from django.db.models import Q
from django.utils import timezone

from apps.associados.models import Associado
from apps.importacao.manual_payment_flags import (
    is_manual_payment_in_cycle,
    is_manual_payment_outside_cycle,
)
from apps.importacao.models import ArquivoRetorno, PagamentoMensalidade
from apps.refinanciamento.models import Comprovante, Refinanciamento
from apps.tesouraria.models import BaixaManual, Pagamento
from core.file_references import build_storage_reference

from .canonicalization import get_operational_contracts_for_associado
from .special_references import is_forced_outside_cycle_reference
from .small_value_rules import (
    blocked_small_value_cycle_status,
    is_return_imported_small_value_contract,
)
from .cycle_timeline import (
    get_contract_activation_payload,
    get_contract_cycle_size,
    get_current_cycle,
    get_cycle_activation_payload,
    get_cycle_request_competencia,
    get_cycle_request_key,
    get_future_generation_threshold,
)
from .models import Ciclo, Contrato, Parcela

PAID_STATUS_CODES = {"1", "4"}
PROJECTION_STATUS_QUITADA = "quitada"
_QUEUE_LOOKUP_DEPTH = 0
ACTIVE_OPERATIONAL_REFINANCIAMENTO_STATUSES = {
    Refinanciamento.Status.APTO_A_RENOVAR,
    Refinanciamento.Status.SOLICITADO_PARA_LIQUIDACAO,
    Refinanciamento.Status.EM_ANALISE_RENOVACAO,
    Refinanciamento.Status.APROVADO_ANALISE_RENOVACAO,
    Refinanciamento.Status.APROVADO_PARA_RENOVACAO,
    Refinanciamento.Status.PENDENTE_APTO,
    Refinanciamento.Status.SOLICITADO,
    Refinanciamento.Status.EM_ANALISE,
    Refinanciamento.Status.APROVADO,
    Refinanciamento.Status.BLOQUEADO,
    # DESATIVADO removido: um refinanciamento cancelado não deve sobrescrever
    # o status visual do ciclo. O status "Inativo" é controlado pelo associado/contrato.
}
APT_LIKE_OPERATIONAL_REFINANCIAMENTO_STATUSES = {
    Refinanciamento.Status.APTO_A_RENOVAR,
    Refinanciamento.Status.PENDENTE_APTO,
    Refinanciamento.Status.SOLICITADO,
}
EFFECTIVE_REFINANCIAMENTO_STATUSES = {
    Refinanciamento.Status.EFETIVADO,
    Refinanciamento.Status.CONCLUIDO,
}
CANCELLED_OPERATIONAL_REFINANCIAMENTO_STATUSES = {
    Refinanciamento.Status.REVERTIDO,
    Refinanciamento.Status.DESATIVADO,
}
STATUS_VISUAL_PHASE_LABELS = {
    "em_analise": "Em Análise",
    "ciclo_aberto": "Ativo",
    "ciclo_com_pendencia": "Com Pendência",
    "inadimplente": "Inadimplente",
    "apto_a_renovar": "Apto para Renovação",
    "solicitado_para_liquidacao": "Solicitado para Liquidação",
    "renovacao_em_analise": "Renovação em Análise",
    "aguardando_coordenacao": "Aguardando Coordenação",
    "aprovado_para_renovacao": "Aguardando Pagamento",
    "ciclo_renovado": "Concluído",
    "contrato_desativado": "Inativo",
    "contrato_encerrado": "Encerrado",
}
STATUS_VISUAL_FINANCIAL_LABELS = {
    "ciclo_em_dia": "Em Dia",
    "ciclo_com_pendencia": "Com Pendência",
    "ciclo_inadimplente": "Inadimplente",
    "ciclo_desativado": "Desativado",
}
STATUS_VISUAL_PHASE_PRIORITY = {
    "aprovado_para_renovacao": 5,
    "solicitado_para_liquidacao": 4,
    "aguardando_coordenacao": 4,
    "renovacao_em_analise": 3,
    "inadimplente": 3,
    "ciclo_com_pendencia": 2,
    "apto_a_renovar": 2,
    "ciclo_aberto": 2,
    "ciclo_renovado": 1,
    "em_analise": 0,
    "contrato_desativado": -1,
    "contrato_encerrado": -2,
}
CONCLUDED_CYCLE_STATUSES = {
    Ciclo.Status.CICLO_RENOVADO,
    Ciclo.Status.FECHADO,
}
OPERATIONAL_STATUS_TO_VISUAL_PHASE = {
    Refinanciamento.Status.APTO_A_RENOVAR: "apto_a_renovar",
    Refinanciamento.Status.SOLICITADO_PARA_LIQUIDACAO: "solicitado_para_liquidacao",
    Refinanciamento.Status.EM_ANALISE_RENOVACAO: "renovacao_em_analise",
    Refinanciamento.Status.APROVADO_ANALISE_RENOVACAO: "aguardando_coordenacao",
    Refinanciamento.Status.APROVADO_PARA_RENOVACAO: "aprovado_para_renovacao",
    Refinanciamento.Status.DESATIVADO: "contrato_desativado",
    Refinanciamento.Status.BLOQUEADO: "renovacao_em_analise",
    Refinanciamento.Status.REJEITADO: "renovacao_em_analise",
}

RESOLVED_UNPAID_STATUSES = {
    Parcela.Status.DESCONTADO,
    Parcela.Status.LIQUIDADA,
    PROJECTION_STATUS_QUITADA,
}
MANUAL_OUTSIDE_CYCLE_HINTS = (
    "fora do ciclo",
    "quitada manualmente",
)


def _is_unresolved_unpaid_row(row: dict[str, object]) -> bool:
    return str(row.get("status") or "") not in RESOLVED_UNPAID_STATUSES


def _is_resolved_cycle_status(value: str | None) -> bool:
    return str(value or "") in RESOLVED_UNPAID_STATUSES


def _is_paid_cycle_status(value: str | None) -> bool:
    return _is_resolved_cycle_status(value)


def _is_current_month_trailing_forecast(
    parcelas: list[dict[str, object]],
    *,
    current_month: date,
) -> bool:
    if not parcelas:
        return False

    unresolved = [
        parcela
        for parcela in parcelas
        if not _is_resolved_cycle_status(str(parcela.get("status") or ""))
    ]
    if len(unresolved) != 1:
        return False

    trailing = unresolved[0]
    trailing_reference = _month_floor(trailing.get("referencia_mes"))
    if (
        str(trailing.get("status") or "") != Parcela.Status.EM_PREVISAO
        or trailing_reference != current_month
    ):
        return False

    latest_reference = max(
        (
            _month_floor(parcela.get("referencia_mes"))
            for parcela in parcelas
            if _month_floor(parcela.get("referencia_mes")) is not None
        ),
        default=None,
    )
    return trailing_reference is not None and trailing_reference == latest_reference


def _renewed_cycle_status(
    parcelas: list[dict[str, object]],
    *,
    current_month: date,
    cycle_size: int,
) -> str:
    paid_count = sum(
        1
        for parcela in parcelas
        if _is_resolved_cycle_status(str(parcela.get("status") or ""))
    )
    if paid_count >= cycle_size:
        return Ciclo.Status.CICLO_RENOVADO
    if _is_current_month_trailing_forecast(parcelas, current_month=current_month):
        return Ciclo.Status.ABERTO
    return Ciclo.Status.PENDENCIA


def _month_floor(value: date | None) -> date | None:
    if value is None:
        return None
    return value.replace(day=1)


def resolve_current_renewal_competencia() -> date:
    current_month = timezone.localdate().replace(day=1)
    ultima_importacao = (
        ArquivoRetorno.objects.filter(status=ArquivoRetorno.Status.CONCLUIDO)
        .order_by("-competencia", "-created_at")
        .first()
    )
    if ultima_importacao is not None and ultima_importacao.competencia is not None:
        return max(ultima_importacao.competencia.replace(day=1), current_month)
    return current_month


@lru_cache(maxsize=12)
def _operational_apt_contract_ids_for_competencia_cached(competencia: date) -> frozenset[int]:
    global _QUEUE_LOOKUP_DEPTH

    from apps.contratos.renovacao import RenovacaoCicloService

    _QUEUE_LOOKUP_DEPTH += 1
    try:
        rows = RenovacaoCicloService.listar_detalhes(
            competencia=competencia,
            status="apto_a_renovar",
        )
        return frozenset(
            int(row["contrato_referencia_renovacao_id"])
            for row in rows
            if row.get("contrato_referencia_renovacao_id") is not None
        )
    finally:
        _QUEUE_LOOKUP_DEPTH -= 1


def invalidate_operational_apt_queue_cache() -> None:
    _operational_apt_contract_ids_for_competencia_cached.cache_clear()


def _operational_apt_contract_ids_for_competencia(competencia: date) -> frozenset[int]:
    if _QUEUE_LOOKUP_DEPTH > 0:
        return frozenset()
    return _operational_apt_contract_ids_for_competencia_cached(competencia)


def _is_contract_in_operational_apt_queue(
    contrato: Contrato,
    *,
    competencia: date | None = None,
) -> bool:
    competencia_resolvida = _month_floor(competencia) or resolve_current_renewal_competencia()
    queue_ids = _operational_apt_contract_ids_for_competencia(competencia_resolvida)
    if not queue_ids:
        return False
    return contrato.id in queue_ids


def _subtract_months(base_date: date, months: int) -> date:
    month_index = base_date.month - 1 - months
    year = base_date.year + month_index // 12
    month = month_index % 12 + 1
    return date(year, month, 1)


def _eligible_paid_references_for_competencia(competencia: date, cycle_size: int) -> list[date]:
    required_count = max(cycle_size - 1, 0)
    return [_subtract_months(competencia, offset) for offset in range(required_count, 0, -1)]


def _has_renewal_progress_for_competencia(contrato: Contrato, competencia: date) -> bool:
    competencia = competencia.replace(day=1)
    refinanciamentos = list(
        contrato.refinanciamentos.filter(competencia_solicitada=competencia).order_by("-created_at", "-id")
    )
    if not refinanciamentos:
        return False

    for refinanciamento in refinanciamentos:
        if (
            refinanciamento.executado_em is not None
            or refinanciamento.ciclo_destino_id is not None
        ):
            return True
        if refinanciamento.status not in {
            Refinanciamento.Status.APTO_A_RENOVAR,
            Refinanciamento.Status.PENDENTE_APTO,
            Refinanciamento.Status.SOLICITADO,
        }:
            return True
    return False


def is_contract_eligible_for_renewal_competencia(
    contrato: Contrato,
    *,
    competencia: date | None = None,
    parcelas: list[dict[str, object]] | None = None,
    projection: dict[str, object] | None = None,
) -> bool:
    if contrato.status not in {Contrato.Status.ATIVO, Contrato.Status.ENCERRADO}:
        return False
    if normalize_associado_mother_status(getattr(contrato.associado, "status", "")) == Associado.Status.INATIVO:
        return False

    competencia_resolvida = _month_floor(competencia) or resolve_current_renewal_competencia()
    if _has_renewal_progress_for_competencia(contrato, competencia_resolvida):
        return False

    projected_parcelas = parcelas
    if projected_parcelas is None:
        resolved_projection = projection or build_contract_cycle_projection(contrato)
        cycles = list(sorted(resolved_projection.get("cycles") or [], key=lambda item: item.get("numero") or 0))
        if not cycles:
            return False
        projected_parcelas = list(cycles[-1].get("parcelas") or [])

    cycle_size = max(len(projected_parcelas), get_contract_cycle_size(contrato))
    expected_references = _eligible_paid_references_for_competencia(competencia_resolvida, cycle_size)
    if not expected_references:
        return False

    parcelas_por_referencia = {
        _month_floor(parcela.get("referencia_mes")): str(parcela.get("status") or "")
        for parcela in projected_parcelas
        if parcela.get("referencia_mes")
    }
    return all(
        parcelas_por_referencia.get(referencia) in RESOLVED_UNPAID_STATUSES
        for referencia in expected_references
    )


@dataclass(frozen=True)
class FinancialReference:
    referencia_mes: date
    status: str
    data_pagamento: date | None
    valor: Decimal
    observacao: str = ""
    source: str = ""
    counts_for_cycle: bool = True
    had_unpaid_event: bool = False
    force_outside_cycle: bool = False


@dataclass(frozen=True)
class EffectiveRenewal:
    refinanciamento: Refinanciamento
    activated_at: datetime
    first_reference: date


def _compose_visual_status(
    *,
    phase_slug: str,
) -> dict[str, str]:
    return {
        "status_visual_slug": phase_slug,
        "status_visual_label": STATUS_VISUAL_PHASE_LABELS[phase_slug],
    }


def normalize_associado_mother_status(raw_status: str | None) -> str:
    status = str(raw_status or "").strip()
    if status in {
        Associado.Status.INATIVO,
        Associado.Status.INADIMPLENTE,
        Associado.Status.APTO_A_RENOVAR,
    }:
        return status
    return Associado.Status.ATIVO


def resolve_associado_mother_status(
    associado: Associado,
    *,
    projections_by_contract: dict[int, dict[str, object]] | None = None,
) -> str:
    normalized_manual_status = normalize_associado_mother_status(associado.status)
    if normalized_manual_status in {
        Associado.Status.INATIVO,
        Associado.Status.INADIMPLENTE,
        Associado.Status.APTO_A_RENOVAR,
    }:
        return normalized_manual_status

    contratos = get_operational_contracts_for_associado(associado)
    if not contratos:
        return Associado.Status.ATIVO

    latest_contract: Contrato | None = None
    latest_cycle: dict[str, object] | None = None
    latest_projection: dict[str, object] | None = None
    for contrato in contratos:
        projection = (
            projections_by_contract.get(contrato.id)
            if projections_by_contract is not None
            else None
        ) or build_contract_cycle_projection(contrato)
        cycles = list(projection.get("cycles") or [])
        if not cycles:
            continue
        cycle = max(cycles, key=lambda item: int(item.get("numero") or 0))
        if latest_cycle is None or int(cycle.get("numero") or 0) >= int(latest_cycle.get("numero") or 0):
            latest_contract = contrato
            latest_cycle = cycle
            latest_projection = projection

    if latest_cycle is None or latest_contract is None:
        return Associado.Status.ATIVO

    latest_cycle_status = str(latest_cycle.get("status") or "")
    latest_phase = str(latest_cycle.get("fase_ciclo") or "")
    latest_status_renovacao = str((latest_projection or {}).get("status_renovacao") or "")
    if latest_cycle_status in CONCLUDED_CYCLE_STATUSES:
        return Associado.Status.ATIVO
    if (
        latest_status_renovacao
        and latest_status_renovacao not in APT_LIKE_OPERATIONAL_REFINANCIAMENTO_STATUSES
    ):
        return Associado.Status.ATIVO
    current_competencia = resolve_current_renewal_competencia()
    operational_apt_contract_ids = _operational_apt_contract_ids_for_competencia(
        current_competencia
    )
    in_operational_apt_queue = latest_contract.id in operational_apt_contract_ids
    canonically_eligible = is_contract_eligible_for_renewal_competencia(
        latest_contract,
        projection=(
            projections_by_contract.get(latest_contract.id)
            if projections_by_contract is not None
            else None
        ),
    )
    projected_as_apto = (
        latest_status_renovacao in APT_LIKE_OPERATIONAL_REFINANCIAMENTO_STATUSES
        or latest_cycle_status == Ciclo.Status.APTO_A_RENOVAR
        or latest_phase == "apto_a_renovar"
    )
    if in_operational_apt_queue and (not projected_as_apto or not canonically_eligible):
        in_operational_apt_queue = False
    if (
        (latest_cycle_status == Ciclo.Status.APTO_A_RENOVAR or latest_phase == "apto_a_renovar")
        and (
            in_operational_apt_queue
            or (not operational_apt_contract_ids and canonically_eligible)
        )
    ):
        return "apto_a_renovar"
    if in_operational_apt_queue:
        return "apto_a_renovar"
    if not operational_apt_contract_ids and canonically_eligible:
        return "apto_a_renovar"
    return Associado.Status.ATIVO


def resolve_associado_status_renovacao(
    associado: Associado,
    *,
    projections_by_contract: dict[int, dict[str, object]] | None = None,
) -> str:
    mother_status = resolve_associado_mother_status(
        associado,
        projections_by_contract=projections_by_contract,
    )
    if mother_status == "apto_a_renovar":
        return Refinanciamento.Status.APTO_A_RENOVAR

    for contrato in get_operational_contracts_for_associado(associado):
        projection = (
            projections_by_contract.get(contrato.id)
            if projections_by_contract is not None
            else None
        ) or build_contract_cycle_projection(contrato)
        status = str(projection.get("status_renovacao") or "")
        if status:
            return status
    return ""


def sync_associado_mother_status(associado: Associado) -> bool:
    invalidate_operational_apt_queue_cache()
    target_status = resolve_associado_mother_status(associado)
    if str(associado.status or "") == target_status:
        return False
    associado.status = target_status
    associado.save(update_fields=["status", "updated_at"])
    return True


def _cycle_financial_status(
    *,
    contrato: Contrato,
    has_unpaid_months: bool,
) -> str:
    associado_status = normalize_associado_mother_status(getattr(contrato.associado, "status", ""))
    if associado_status == Associado.Status.INATIVO or contrato.status == Contrato.Status.CANCELADO:
        return "ciclo_desativado"
    if has_unpaid_months:
        return "ciclo_com_pendencia"
    return "ciclo_em_dia"


def _cycle_phase_status(
    *,
    contrato: Contrato,
    cycle_status: str,
    next_renewal: EffectiveRenewal | None,
    refinanciamento_operacional: Refinanciamento | None,
) -> str:
    associado_status = normalize_associado_mother_status(getattr(contrato.associado, "status", ""))
    if associado_status == Associado.Status.INATIVO or contrato.status == Contrato.Status.CANCELADO:
        return "contrato_desativado"
    if contrato.status == Contrato.Status.ENCERRADO:
        return "contrato_encerrado"
    if cycle_status == Ciclo.Status.PENDENCIA:
        return "ciclo_com_pendencia"
    if cycle_status in CONCLUDED_CYCLE_STATUSES:
        return "ciclo_renovado"
    if refinanciamento_operacional is not None:
        normalized = _normalize_operational_status(refinanciamento_operacional.status)
        if normalized in OPERATIONAL_STATUS_TO_VISUAL_PHASE:
            return OPERATIONAL_STATUS_TO_VISUAL_PHASE[normalized]
    if (
        cycle_status == Ciclo.Status.APTO_A_RENOVAR
        and has_cancelled_operational_refinanciamento(contrato)
    ):
        return "ciclo_aberto"
    if cycle_status == Ciclo.Status.APTO_A_RENOVAR:
        return "apto_a_renovar"
    return "ciclo_aberto"


def _cycle_visual_rank(cycle: dict[str, object]) -> tuple[int, int, int]:
    phase_slug = str(cycle.get("fase_ciclo") or "")
    return (
        STATUS_VISUAL_PHASE_PRIORITY.get(phase_slug, -1),
        int(cycle.get("numero") or 0),
    )


def _fallback_visual_status_from_models(
    *,
    associado_status: str,
    contrato_status: str,
) -> dict[str, str]:
    normalized_status = normalize_associado_mother_status(associado_status)
    if normalized_status == Associado.Status.INATIVO or contrato_status == Contrato.Status.CANCELADO:
        return _compose_visual_status(phase_slug="contrato_desativado")
    if normalized_status == "apto_a_renovar":
        return _compose_visual_status(phase_slug="apto_a_renovar")
    if contrato_status == Contrato.Status.ENCERRADO:
        return _compose_visual_status(phase_slug="contrato_encerrado")
    if (
        normalized_status == Associado.Status.ATIVO
        and contrato_status == Contrato.Status.ATIVO
    ):
        return _compose_visual_status(phase_slug="ciclo_aberto")
    return _compose_visual_status(phase_slug="em_analise")


def _month_start(value: date | None) -> date | None:
    if value is None:
        return None
    return value.replace(day=1)


def _add_months(base_date: date, months: int) -> date:
    month_index = base_date.month - 1 + months
    year = base_date.year + month_index // 12
    month = month_index % 12 + 1
    return date(year, month, 1)


def _projection_id(*, contrato_id: int, cycle_number: int, slot: int) -> int:
    return -((contrato_id * 1000) + (cycle_number * 10) + slot)


def _projection_cycle_id(*, contrato_id: int, cycle_number: int) -> int:
    return -((contrato_id * 100) + cycle_number)


def _normalize_document(value: str | None) -> str:
    return "".join(char for char in (value or "") if char.isdigit())


def _is_paid_pagamento(pagamento: PagamentoMensalidade) -> bool:
    return bool(
        (pagamento.status_code or "").strip() in PAID_STATUS_CODES
        or pagamento.manual_status == PagamentoMensalidade.ManualStatus.PAGO
    )


def _is_canceled_pagamento(pagamento: PagamentoMensalidade) -> bool:
    return pagamento.manual_status == PagamentoMensalidade.ManualStatus.CANCELADO


def _is_manual_regularized_pagamento(pagamento: PagamentoMensalidade) -> bool:
    return pagamento.manual_status == PagamentoMensalidade.ManualStatus.PAGO


def _manual_regularized_counts_for_cycle(pagamento: PagamentoMensalidade) -> bool | None:
    if not _is_manual_regularized_pagamento(pagamento):
        return None
    if is_manual_payment_in_cycle(pagamento.manual_forma_pagamento):
        return True
    if is_manual_payment_outside_cycle(pagamento.manual_forma_pagamento):
        return False
    return None


def _pagamento_paid_date(pagamento: PagamentoMensalidade) -> date:
    if pagamento.manual_paid_at:
        return pagamento.manual_paid_at.date()
    timestamp = pagamento.updated_at or pagamento.created_at or timezone.now()
    return timestamp.date()


def _pagamento_record_key(pagamento: PagamentoMensalidade) -> tuple[object, ...]:
    if _is_manual_regularized_pagamento(pagamento) or _is_paid_pagamento(pagamento):
        logical_order = 1
    else:
        logical_order = 0
    return (
        _month_start(pagamento.referencia_month),
        logical_order,
        pagamento.manual_paid_at or pagamento.updated_at or pagamento.created_at,
        pagamento.id,
    )


def _prefetched_pagamentos(contrato: Contrato) -> list[PagamentoMensalidade] | None:
    prefetched = getattr(contrato.associado, "_prefetched_objects_cache", {})
    pagamentos = prefetched.get("pagamentos_mensalidades")
    if pagamentos is None:
        return None
    return list(pagamentos)


def _query_pagamentos(contrato: Contrato) -> list[PagamentoMensalidade]:
    cpf_cnpj = _normalize_document(contrato.associado.cpf_cnpj)
    return list(
        PagamentoMensalidade.objects.filter(associado=contrato.associado)
        .union(
            PagamentoMensalidade.objects.filter(cpf_cnpj=cpf_cnpj),
            all=True,
        )
        .order_by("referencia_month", "id")
    )


def _query_baixas(contrato: Contrato) -> list[BaixaManual]:
    return list(
        BaixaManual.objects.select_related("parcela")
        .filter(parcela__associado=contrato.associado)
        .order_by("data_baixa", "id")
    )


def _query_current_parcelas(contrato: Contrato) -> list[Parcela]:
    prefetched_cycles = getattr(contrato, "_prefetched_objects_cache", {}).get("ciclos")
    if prefetched_cycles is not None:
        parcelas: list[Parcela] = []
        for ciclo in prefetched_cycles:
            prefetched_parcelas = getattr(ciclo, "_prefetched_objects_cache", {}).get(
                "parcelas"
            )
            if prefetched_parcelas is not None:
                parcelas.extend(list(prefetched_parcelas))
            else:
                parcelas.extend(list(ciclo.parcelas.all()))
        return parcelas
    return list(
        Parcela.all_objects.filter(ciclo__contrato=contrato, deleted_at__isnull=True)
        .exclude(status=Parcela.Status.CANCELADO)
        .select_related("ciclo")
    )


def _renewal_origin_refs(refinanciamento: Refinanciamento) -> list[date]:
    return sorted(
        reference
        for reference in [
            refinanciamento.ref1,
            refinanciamento.ref2,
            refinanciamento.ref3,
            refinanciamento.ref4,
        ]
        if reference is not None
    )


def get_contract_baseline_reference(
    contrato: Contrato,
    *,
    refs: list[FinancialReference] | None = None,
) -> date | None:
    if refs is None:
        paid, unpaid, regularized = _merge_financial_references(contrato)
        refs = [*paid, *unpaid, *regularized]

    candidates: list[date] = []
    if refs:
        candidates.append(refs[0].referencia_mes)
    for raw_value in [
        contrato.mes_averbacao,
        contrato.auxilio_liberado_em,
        contrato.data_primeira_mensalidade,
    ]:
        month_value = _month_start(raw_value)
        if month_value is not None:
            candidates.append(month_value)
    if contrato.data_aprovacao:
        candidates.append(_month_start(contrato.data_aprovacao))
    return min(candidates) if candidates else None


def refinanciamento_matches_contract_timeline(
    contrato: Contrato,
    refinanciamento: Refinanciamento,
    *,
    refs: list[FinancialReference] | None = None,
) -> bool:
    if refinanciamento.legacy_refinanciamento_id is None:
        return True
    if (refinanciamento.contrato_codigo_origem or "").strip():
        return True
    origin_refs = _renewal_origin_refs(refinanciamento)
    if not origin_refs:
        return True
    baseline = get_contract_baseline_reference(contrato, refs=refs)
    if baseline is None:
        return True
    if origin_refs[0].replace(day=1) >= baseline:
        return True
    return _legacy_renewal_has_treasury_payment_proof(
        contrato,
        refinanciamento,
    )


def _reference_sort_key(item: FinancialReference) -> tuple[date, date | None]:
    return item.referencia_mes, item.data_pagamento


def _merge_financial_references(
    contrato: Contrato,
) -> tuple[
    list[FinancialReference],
    list[FinancialReference],
    list[FinancialReference],
]:
    pagamentos = _prefetched_pagamentos(contrato)
    if pagamentos is None:
        pagamentos = _query_pagamentos(contrato)
    baixas = _query_baixas(contrato)
    fallback_parcelas = _query_current_parcelas(contrato)
    materialized_paid_by_reference = {
        _month_start(parcela.referencia_mes): parcela
        for parcela in fallback_parcelas
        if _month_start(parcela.referencia_mes) is not None
        and parcela.status in {Parcela.Status.DESCONTADO, Parcela.Status.LIQUIDADA}
    }

    paid_by_reference: dict[date, FinancialReference] = {}
    unpaid_by_reference: dict[date, FinancialReference] = {}
    references_with_unpaid_history: set[date] = set()

    for pagamento in sorted(pagamentos, key=_pagamento_record_key):
        referencia = _month_start(pagamento.referencia_month)
        if referencia is None:
            continue
        forced_outside_cycle = is_forced_outside_cycle_reference(referencia)
        if _is_canceled_pagamento(pagamento):
            paid_by_reference.pop(referencia, None)
            unpaid_by_reference.pop(referencia, None)
            continue
        if _is_manual_regularized_pagamento(pagamento):
            manual_counts_for_cycle = _manual_regularized_counts_for_cycle(pagamento)
            materialized_paid = materialized_paid_by_reference.get(referencia)
            if manual_counts_for_cycle is True and not forced_outside_cycle:
                paid_by_reference[referencia] = FinancialReference(
                    referencia_mes=referencia,
                    status=(
                        materialized_paid.status
                        if materialized_paid is not None
                        else Parcela.Status.DESCONTADO
                    ),
                    data_pagamento=(
                        materialized_paid.data_pagamento
                        if materialized_paid is not None and materialized_paid.data_pagamento
                        else _pagamento_paid_date(pagamento)
                    ),
                    valor=pagamento.recebido_manual
                    or pagamento.valor
                    or (
                        materialized_paid.valor if materialized_paid is not None else None
                    )
                    or contrato.valor_mensalidade,
                    observacao=(
                        materialized_paid.observacao
                        if materialized_paid is not None and materialized_paid.observacao
                        else "Competência quitada manualmente e integrada ao ciclo."
                    ),
                    source="manual_regularized",
                    counts_for_cycle=True,
                    had_unpaid_event=False,
                )
                unpaid_by_reference.pop(referencia, None)
                continue
            if manual_counts_for_cycle is False or forced_outside_cycle:
                paid_by_reference[referencia] = FinancialReference(
                    referencia_mes=referencia,
                    status=PROJECTION_STATUS_QUITADA,
                    data_pagamento=_pagamento_paid_date(pagamento),
                    valor=pagamento.recebido_manual
                    or pagamento.valor
                    or (
                        materialized_paid.valor if materialized_paid is not None else None
                    )
                    or contrato.valor_mensalidade,
                    observacao="Competência quitada manualmente fora do ciclo.",
                    source="manual_regularized",
                    counts_for_cycle=False,
                    had_unpaid_event=True,
                    force_outside_cycle=True,
                )
                unpaid_by_reference.pop(referencia, None)
                references_with_unpaid_history.add(referencia)
                continue
            if materialized_paid is not None and not forced_outside_cycle:
                paid_by_reference[referencia] = FinancialReference(
                    referencia_mes=referencia,
                    status=materialized_paid.status,
                    data_pagamento=materialized_paid.data_pagamento
                    or _pagamento_paid_date(pagamento),
                    valor=pagamento.recebido_manual
                    or pagamento.valor
                    or materialized_paid.valor
                    or contrato.valor_mensalidade,
                    observacao=materialized_paid.observacao or "Quitado via ajuste manual.",
                    source="manual_regularized",
                    counts_for_cycle=True,
                    had_unpaid_event=False,
                )
                unpaid_by_reference.pop(referencia, None)
                continue
            paid_by_reference[referencia] = FinancialReference(
                referencia_mes=referencia,
                status=PROJECTION_STATUS_QUITADA,
                data_pagamento=_pagamento_paid_date(pagamento),
                valor=pagamento.recebido_manual
                or pagamento.valor
                or contrato.valor_mensalidade,
                observacao="Competência quitada manualmente fora do ciclo.",
                source="manual_regularized",
                counts_for_cycle=False,
                had_unpaid_event=True,
                force_outside_cycle=forced_outside_cycle,
            )
            unpaid_by_reference.pop(referencia, None)
            references_with_unpaid_history.add(referencia)
            continue
        if _is_paid_pagamento(pagamento):
            paid_at = _pagamento_paid_date(pagamento)
            had_unpaid_event = referencia in references_with_unpaid_history or forced_outside_cycle
            paid_by_reference[referencia] = FinancialReference(
                referencia_mes=referencia,
                status=(
                    PROJECTION_STATUS_QUITADA
                    if had_unpaid_event
                    else Parcela.Status.DESCONTADO
                ),
                data_pagamento=paid_at,
                valor=pagamento.valor or contrato.valor_mensalidade,
                observacao=(
                    "Competência quitada após atraso e mantida fora do ciclo."
                    if had_unpaid_event
                    else "Quitado via importação/ajuste manual."
                ),
                source="pagamento_mensalidade",
                counts_for_cycle=not had_unpaid_event,
                had_unpaid_event=had_unpaid_event,
                force_outside_cycle=forced_outside_cycle,
            )
            unpaid_by_reference.pop(referencia, None)
            continue

        references_with_unpaid_history.add(referencia)
        unpaid_by_reference[referencia] = FinancialReference(
            referencia_mes=referencia,
            status=Parcela.Status.NAO_DESCONTADO,
            data_pagamento=None,
            valor=pagamento.valor or contrato.valor_mensalidade,
            observacao="Competência não quitada no retorno.",
            source="pagamento_mensalidade",
            counts_for_cycle=False,
            had_unpaid_event=True,
            force_outside_cycle=forced_outside_cycle,
        )
        paid_by_reference.pop(referencia, None)

    for baixa in baixas:
        referencia = _month_start(getattr(baixa.parcela, "referencia_mes", None))
        if referencia is None:
            continue
        materialized_paid = materialized_paid_by_reference.get(referencia)
        if materialized_paid is not None:
            paid_by_reference[referencia] = FinancialReference(
                referencia_mes=referencia,
                status=(
                    PROJECTION_STATUS_QUITADA
                    if is_forced_outside_cycle_reference(referencia)
                    else materialized_paid.status
                ),
                data_pagamento=materialized_paid.data_pagamento or baixa.data_baixa,
                valor=baixa.valor_pago or materialized_paid.valor or contrato.valor_mensalidade,
                observacao=(
                    baixa.observacao
                    or materialized_paid.observacao
                    or "Competência quitada por baixa manual fora do ciclo."
                ),
                source="baixa_manual",
                counts_for_cycle=not is_forced_outside_cycle_reference(referencia),
                had_unpaid_event=is_forced_outside_cycle_reference(referencia),
                force_outside_cycle=is_forced_outside_cycle_reference(referencia),
            )
            unpaid_by_reference.pop(referencia, None)
            continue
        had_unpaid_event = True
        paid_by_reference[referencia] = FinancialReference(
            referencia_mes=referencia,
            status=PROJECTION_STATUS_QUITADA,
            data_pagamento=baixa.data_baixa,
            valor=baixa.valor_pago or contrato.valor_mensalidade,
            observacao=baixa.observacao or "Competência quitada por baixa manual fora do ciclo.",
            source="baixa_manual",
            counts_for_cycle=False,
            had_unpaid_event=had_unpaid_event,
            force_outside_cycle=is_forced_outside_cycle_reference(referencia),
        )
        unpaid_by_reference.pop(referencia, None)
        references_with_unpaid_history.add(referencia)

    for parcela in fallback_parcelas:
        referencia = _month_start(parcela.referencia_mes)
        if referencia is None or referencia in paid_by_reference or referencia in unpaid_by_reference:
            continue
        if parcela.status == Parcela.Status.NAO_DESCONTADO:
            references_with_unpaid_history.add(referencia)
            unpaid_by_reference[referencia] = FinancialReference(
                referencia_mes=referencia,
                status=Parcela.Status.NAO_DESCONTADO,
                data_pagamento=None,
                valor=parcela.valor,
                observacao=parcela.observacao,
                source="parcela_fallback",
                counts_for_cycle=False,
                had_unpaid_event=True,
            )
        elif parcela.status == Parcela.Status.LIQUIDADA:
            paid_by_reference[referencia] = FinancialReference(
                referencia_mes=referencia,
                status=(
                    PROJECTION_STATUS_QUITADA
                    if is_forced_outside_cycle_reference(referencia)
                    else Parcela.Status.LIQUIDADA
                ),
                data_pagamento=parcela.data_pagamento,
                valor=parcela.valor,
                observacao=parcela.observacao or "Parcela liquidada pela tesouraria.",
                source="parcela_liquidada",
                counts_for_cycle=not is_forced_outside_cycle_reference(referencia),
                had_unpaid_event=is_forced_outside_cycle_reference(referencia),
                force_outside_cycle=is_forced_outside_cycle_reference(referencia),
            )
        elif parcela.status == Parcela.Status.DESCONTADO:
            had_unpaid_event = (
                referencia in references_with_unpaid_history
                or is_forced_outside_cycle_reference(referencia)
            )
            paid_by_reference[referencia] = FinancialReference(
                referencia_mes=referencia,
                status=(
                    PROJECTION_STATUS_QUITADA
                    if had_unpaid_event
                    else Parcela.Status.DESCONTADO
                ),
                data_pagamento=parcela.data_pagamento,
                valor=parcela.valor,
                observacao=(
                    parcela.observacao
                    or (
                        "Competência quitada fora do ciclo."
                        if had_unpaid_event
                        else ""
                    )
                ),
                source="parcela_fallback",
                counts_for_cycle=not had_unpaid_event,
                had_unpaid_event=had_unpaid_event,
                force_outside_cycle=is_forced_outside_cycle_reference(referencia),
            )

    known_references = {
        *paid_by_reference.keys(),
        *unpaid_by_reference.keys(),
    }
    for current_reference, next_reference in zip(
        sorted(known_references),
        sorted(known_references)[1:],
    ):
        probe = _add_months(current_reference, 1)
        while probe < next_reference:
            if probe not in known_references:
                unpaid_by_reference[probe] = FinancialReference(
                    referencia_mes=probe,
                    status=Parcela.Status.NAO_DESCONTADO,
                    data_pagamento=None,
                    valor=contrato.valor_mensalidade,
                    observacao="Competência vencida sem registro no retorno.",
                    source="implicit_gap",
                    counts_for_cycle=False,
                    had_unpaid_event=True,
                )
                references_with_unpaid_history.add(probe)
                known_references.add(probe)
            probe = _add_months(probe, 1)

    paid_in_cycle = sorted(
        [
            item
            for item in paid_by_reference.values()
            if item.counts_for_cycle
        ],
        key=_reference_sort_key,
    )
    unresolved_unpaid = sorted(unpaid_by_reference.values(), key=_reference_sort_key)
    regularized_outside_cycle = sorted(
        [
            item
            for item in paid_by_reference.values()
            if not item.counts_for_cycle
        ],
        key=_reference_sort_key,
    )
    return paid_in_cycle, unresolved_unpaid, regularized_outside_cycle


def _seed_reference(
    contrato: Contrato,
    refs: list[FinancialReference],
    renewals: list[EffectiveRenewal],
) -> date:
    candidates: list[date] = []
    if refs:
        candidates.append(refs[0].referencia_mes)
    baseline = get_contract_baseline_reference(contrato, refs=refs)
    renewal_origin_refs = sorted(
        reference
        for renewal in renewals
        for reference in _renewal_origin_refs(renewal.refinanciamento)
        if baseline is None or reference.replace(day=1) >= baseline
    )
    if renewal_origin_refs:
        candidates.append(renewal_origin_refs[0].replace(day=1))
    if contrato.mes_averbacao:
        candidates.append(contrato.mes_averbacao.replace(day=1))
    if contrato.data_primeira_mensalidade:
        candidates.append(contrato.data_primeira_mensalidade.replace(day=1))
    if contrato.data_aprovacao:
        candidates.append(_add_months(contrato.data_aprovacao.replace(day=1), 1))
    if candidates:
        seed = min(candidates)
        # Para contratos de reativação, pagamentos do contrato anterior (antes de
        # auxilio_liberado_em) não devem ancorar o ciclo da reativação. Garantir
        # que o seed não seja anterior ao mês em que o auxílio foi liberado.
        if (
            str(getattr(contrato, "origem_operacional", "") or "") == "reativacao"
            and contrato.auxilio_liberado_em is not None
        ):
            reativacao_start = _month_start(contrato.auxilio_liberado_em)
            if reativacao_start is not None and seed < reativacao_start:
                seed = reativacao_start
        return seed
    return timezone.localdate().replace(day=1)


def _contract_is_activated(contrato: Contrato, refs: list[FinancialReference]) -> bool:
    payload = get_contract_activation_payload(contrato)
    if payload["data_primeiro_ciclo_ativado"] is not None:
        return True
    if contrato.auxilio_liberado_em is not None:
        return True
    if contrato.status in {Contrato.Status.ATIVO, Contrato.Status.ENCERRADO}:
        return True
    return bool(refs)


def _effective_renewal_activation(refinanciamento: Refinanciamento) -> datetime | None:
    return (
        refinanciamento.data_ativacao_ciclo
        or refinanciamento.executado_em
        or (refinanciamento.created_at if refinanciamento.legacy_refinanciamento_id else None)
    )


def _prefetched_tesouraria_pagamentos(contrato: Contrato) -> list[Pagamento] | None:
    prefetched = getattr(contrato.associado, "_prefetched_objects_cache", {})
    pagamentos = prefetched.get("tesouraria_pagamentos")
    if pagamentos is None:
        return None
    return list(pagamentos)


def _query_tesouraria_pagamentos(contrato: Contrato) -> list[Pagamento]:
    return list(
        Pagamento.all_objects.filter(
            cadastro=contrato.associado,
            status=Pagamento.Status.PAGO,
        ).order_by("paid_at", "created_at", "id")
    )


def _legacy_renewal_has_treasury_payment_proof(
    contrato: Contrato,
    refinanciamento: Refinanciamento,
    *,
    activated_at: datetime | None = None,
) -> bool:
    if refinanciamento.legacy_refinanciamento_id is None:
        return False

    activation_point = activated_at or _effective_renewal_activation(refinanciamento)
    if activation_point is None:
        return False

    pagamentos = _prefetched_tesouraria_pagamentos(contrato)
    if pagamentos is None:
        pagamentos = _query_tesouraria_pagamentos(contrato)

    activation_reference = activation_point.date().replace(day=1)
    expected_value = contrato.valor_mensalidade or Decimal("0")
    for pagamento in pagamentos:
        if pagamento.status != Pagamento.Status.PAGO or pagamento.paid_at is None:
            continue
        if pagamento.contrato_codigo and pagamento.contrato_codigo != contrato.codigo:
            continue
        payment_reference = pagamento.paid_at.date().replace(day=1)
        if payment_reference != activation_reference:
            continue
        if abs(pagamento.paid_at - activation_point) > timedelta(days=21):
            continue
        payment_value = pagamento.valor_pago or Decimal("0")
        if expected_value and payment_value:
            if abs(payment_value - expected_value) > Decimal("0.01"):
                continue
        notes = (pagamento.notes or "").lower()
        forma_pagamento = (pagamento.forma_pagamento or "").lower()
        if not (
            "arquivo retorno" in notes
            or "coordenador" in notes
            or "renova" in notes
            or forma_pagamento in {"manual", "pix", "dinheiro"}
        ):
            continue
        return True
    return False


def _effective_renewal_first_reference(
    contrato: Contrato,
    refinanciamento: Refinanciamento,
    activated_at: datetime,
) -> date:
    origin_refs = _renewal_origin_refs(refinanciamento)
    activation_reference = activated_at.date().replace(day=1)
    cycle_size = get_contract_cycle_size(contrato)
    if origin_refs:
        next_reference = _add_months(origin_refs[-1].replace(day=1), 1)
        if (
            len(origin_refs) >= max(cycle_size - 1, 1)
            and activation_reference == next_reference
        ):
            # When the renewal is efetivated in the same month as the trailing
            # competence that still belongs to the origin cycle, keep that
            # competence inside the current cycle and start the next cycle only
            # in the following month.
            candidate = _add_months(next_reference, 1)
        elif _legacy_renewal_has_treasury_payment_proof(
            contrato,
            refinanciamento,
            activated_at=activated_at,
        ):
            candidate = max(next_reference, activation_reference)
        else:
            candidate = next_reference
        return candidate
    else:
        candidate = activation_reference

    occupied_references = {
        parcela.referencia_mes.replace(day=1)
        for parcela in _query_current_parcelas(contrato)
        if parcela.ciclo_id != refinanciamento.ciclo_destino_id
    }
    safety_limit = 24
    while safety_limit > 0 and any(
        _add_months(candidate, offset) in occupied_references
        for offset in range(cycle_size)
    ):
        candidate = _add_months(candidate, 1)
        safety_limit -= 1
    return candidate


def _is_effective_renewal(refinanciamento: Refinanciamento) -> bool:
    if refinanciamento.deleted_at is not None:
        return False
    if refinanciamento.legacy_refinanciamento_id is not None:
        return _effective_renewal_activation(refinanciamento) is not None
    return bool(
        refinanciamento.ciclo_destino_id is not None
        or refinanciamento.executado_em is not None
        or refinanciamento.data_ativacao_ciclo is not None
    )


def _effective_renewals(
    contrato: Contrato,
    *,
    refs: list[FinancialReference] | None = None,
) -> list[EffectiveRenewal]:
    prefetched = getattr(contrato.associado, "_prefetched_objects_cache", {}).get(
        "refinanciamentos"
    )
    if prefetched is not None:
        base = [item for item in prefetched if item.contrato_origem_id == contrato.id]
    else:
        base = list(
            Refinanciamento.objects.filter(contrato_origem=contrato)
            .select_related("ciclo_origem", "ciclo_destino")
            .order_by("created_at", "id")
        )

    renewals: list[EffectiveRenewal] = []
    for refinanciamento in base:
        if not _is_effective_renewal(refinanciamento):
            continue
        if not refinanciamento_matches_contract_timeline(
            contrato,
            refinanciamento,
            refs=refs,
        ):
            continue
        activated_at = _effective_renewal_activation(refinanciamento)
        if activated_at is None:
            continue
        renewals.append(
            EffectiveRenewal(
                refinanciamento=refinanciamento,
                activated_at=activated_at,
                first_reference=_effective_renewal_first_reference(
                    contrato,
                    refinanciamento,
                    activated_at,
                ),
            )
        )
    renewals.sort(key=lambda item: (item.activated_at, item.refinanciamento.id))
    return renewals


def get_contract_materialized_cycle_count(contrato: Contrato) -> int:
    paid, unpaid, regularized = _merge_financial_references(contrato)
    if not _contract_is_activated(contrato, [*paid, *unpaid, *regularized]):
        return 0
    return 1 + len(_effective_renewals(contrato, refs=[*paid, *unpaid, *regularized]))


def _ranked_operational_refinanciamentos(contrato: Contrato) -> list[Refinanciamento]:
    prefetched = getattr(contrato.associado, "_prefetched_objects_cache", {}).get(
        "refinanciamentos"
    )
    if prefetched is not None:
        base = [item for item in prefetched if item.contrato_origem_id == contrato.id]
    else:
        base = list(
            Refinanciamento.objects.filter(contrato_origem=contrato).order_by(
                "-created_at", "-id"
            )
        )
    filtered = [
        item
        for item in base
        if item.deleted_at is None and item.legacy_refinanciamento_id is None
    ]
    if not filtered:
        return []

    def status_priority(item: Refinanciamento) -> tuple[int, datetime, int]:
        normalized = _normalize_operational_status(item.status)
        priority_map = {
            Refinanciamento.Status.EFETIVADO: 100,
            Refinanciamento.Status.CONCLUIDO: 90,
            Refinanciamento.Status.APROVADO_PARA_RENOVACAO: 80,
            Refinanciamento.Status.APROVADO_ANALISE_RENOVACAO: 70,
            Refinanciamento.Status.PENDENTE_TERMO_ANALISTA: 60,
            Refinanciamento.Status.EM_ANALISE_RENOVACAO: 50,
            Refinanciamento.Status.PENDENTE_TERMO_AGENTE: 40,
            Refinanciamento.Status.SOLICITADO_PARA_LIQUIDACAO: 35,
            Refinanciamento.Status.BLOQUEADO: 30,
            Refinanciamento.Status.REVERTIDO: 25,
            Refinanciamento.Status.DESATIVADO: 20,
            Refinanciamento.Status.APTO_A_RENOVAR: 10,
        }
        marker = (
            item.executado_em
            or item.data_ativacao_ciclo
            or item.updated_at
            or item.created_at
            or timezone.make_aware(datetime.min)
        )
        return (priority_map.get(normalized, 0), marker, item.id)

    best_by_competencia: dict[date, Refinanciamento] = {}
    for item in filtered:
        competencia = _month_floor(item.competencia_solicitada) or date.min
        current = best_by_competencia.get(competencia)
        if current is None or status_priority(item) > status_priority(current):
            best_by_competencia[competencia] = item

    return sorted(
        best_by_competencia.values(),
        key=lambda candidate: (
            _month_floor(candidate.competencia_solicitada) or date.min,
            status_priority(candidate),
        ),
        reverse=True,
    )


def _active_operational_refinanciamentos(contrato: Contrato) -> list[Refinanciamento]:
    return [
        item
        for item in _ranked_operational_refinanciamentos(contrato)
        if item.status in ACTIVE_OPERATIONAL_REFINANCIAMENTO_STATUSES
        and not _is_effective_renewal(item)
    ]


def _latest_operational_refinanciamento(contrato: Contrato) -> Refinanciamento | None:
    for item in _ranked_operational_refinanciamentos(contrato):
        if item.status in ACTIVE_OPERATIONAL_REFINANCIAMENTO_STATUSES and not _is_effective_renewal(item):
            return item
        if item.status in EFFECTIVE_REFINANCIAMENTO_STATUSES and _is_effective_renewal(item):
            return item
    return None


def has_cancelled_operational_refinanciamento(
    contrato: Contrato,
    *,
    cycle_key: str | None = None,
    competencia: date | None = None,
) -> bool:
    if not cycle_key and competencia is None:
        current_cycle = get_current_cycle(contrato)
        if current_cycle is None:
            return False
        cycle_key = get_cycle_request_key(current_cycle)
        competencia = get_cycle_request_competencia(current_cycle)

    queryset = Refinanciamento.objects.filter(
        contrato_origem=contrato,
        deleted_at__isnull=True,
        legacy_refinanciamento_id__isnull=True,
        origem=Refinanciamento.Origem.OPERACIONAL,
        ciclo_destino__isnull=True,
        executado_em__isnull=True,
        data_ativacao_ciclo__isnull=True,
        status__in=CANCELLED_OPERATIONAL_REFINANCIAMENTO_STATUSES,
    )
    if cycle_key and competencia is not None:
        queryset = queryset.filter(
            Q(cycle_key=cycle_key) | Q(competencia_solicitada=competencia)
        )
    elif cycle_key:
        queryset = queryset.filter(cycle_key=cycle_key)
    elif competencia is not None:
        queryset = queryset.filter(competencia_solicitada=competencia)
    else:
        return False
    return queryset.exists()


def _normalize_operational_status(status: str) -> str:
    mapping = {
        Refinanciamento.Status.PENDENTE_APTO: Refinanciamento.Status.APTO_A_RENOVAR,
        Refinanciamento.Status.SOLICITADO: Refinanciamento.Status.APTO_A_RENOVAR,
        Refinanciamento.Status.EM_ANALISE: Refinanciamento.Status.EM_ANALISE_RENOVACAO,
        Refinanciamento.Status.APROVADO: Refinanciamento.Status.APROVADO_ANALISE_RENOVACAO,
        Refinanciamento.Status.CONCLUIDO: Refinanciamento.Status.APROVADO_ANALISE_RENOVACAO,
    }
    return mapping.get(status, status)


def _resolve_storage_reference_with_fallback(
    *candidates: str | None,
    missing_type: str,
    local_type: str = "local",
):
    normalized_candidates: list[str] = []
    for candidate in candidates:
        normalized = str(candidate or "").strip()
        if normalized and normalized not in normalized_candidates:
            normalized_candidates.append(normalized)

    if not normalized_candidates:
        return build_storage_reference(
            "",
            missing_type=missing_type,
            local_type=local_type,
        )

    last_reference = build_storage_reference(
        normalized_candidates[0],
        missing_type=missing_type,
        local_type=local_type,
    )
    if last_reference.arquivo_disponivel_localmente:
        return last_reference

    for candidate in normalized_candidates[1:]:
        fallback_reference = build_storage_reference(
            candidate,
            missing_type=missing_type,
            local_type=local_type,
        )
        if fallback_reference.arquivo_disponivel_localmente:
            return fallback_reference
        last_reference = fallback_reference

    return last_reference


def _serialize_comprovante(comprovante: Comprovante) -> dict[str, object]:
    arquivo_path = str(getattr(comprovante.arquivo, "name", "") or "")
    arquivo_referencia = comprovante.arquivo_referencia or arquivo_path
    reference = _resolve_storage_reference_with_fallback(
        arquivo_referencia,
        arquivo_path,
        missing_type=(
            "legado_sem_arquivo"
            if comprovante.legacy_comprovante_id is not None
            or comprovante.origem == Comprovante.Origem.LEGADO
            else "referencia_path"
        ),
        local_type="local",
    )
    created_at = (
        (
            getattr(comprovante.refinanciamento, "data_ativacao_ciclo", None)
            if comprovante.refinanciamento_id
            else None
        )
        or (
            getattr(comprovante.refinanciamento, "executado_em", None)
            if comprovante.refinanciamento_id
            else None
        )
        or comprovante.data_pagamento
        or comprovante.created_at
    )
    return {
        "id": comprovante.id,
        "tipo": comprovante.tipo,
        "papel": comprovante.papel,
        "arquivo": reference.url or arquivo_path or arquivo_referencia,
        "arquivo_referencia": reference.arquivo_referencia or arquivo_referencia,
        "arquivo_disponivel_localmente": reference.arquivo_disponivel_localmente,
        "tipo_referencia": reference.tipo_referencia,
        "nome_original": comprovante.nome_original,
        "mime": comprovante.mime,
        "size_bytes": comprovante.size_bytes,
        "data_pagamento": comprovante.data_pagamento,
        "origem": comprovante.origem,
        "status_validacao": comprovante.status_validacao,
        "created_at": created_at,
        "legacy_comprovante_id": comprovante.legacy_comprovante_id,
    }


def _synthesize_termo_payload(refinanciamento: Refinanciamento) -> dict[str, object] | None:
    if not refinanciamento.termo_antecipacao_path:
        return None
    activation_at = (
        refinanciamento.data_ativacao_ciclo
        or refinanciamento.executado_em
        or refinanciamento.created_at
    )
    reference = build_storage_reference(
        refinanciamento.termo_antecipacao_path,
        missing_type=(
            "legado_sem_arquivo"
            if refinanciamento.legacy_refinanciamento_id is not None
            else "referencia_path"
        ),
        local_type="local",
    )
    return {
        "id": None,
        "tipo": Comprovante.Tipo.TERMO_ANTECIPACAO,
        "papel": Comprovante.Papel.OPERACIONAL,
        "arquivo": reference.url or refinanciamento.termo_antecipacao_path,
        "arquivo_referencia": reference.arquivo_referencia or refinanciamento.termo_antecipacao_path,
        "arquivo_disponivel_localmente": reference.arquivo_disponivel_localmente,
        "tipo_referencia": reference.tipo_referencia,
        "nome_original": refinanciamento.termo_antecipacao_original_name,
        "mime": refinanciamento.termo_antecipacao_mime,
        "size_bytes": refinanciamento.termo_antecipacao_size_bytes,
        "data_pagamento": activation_at,
        "origem": (
            Comprovante.Origem.LEGADO
            if refinanciamento.legacy_refinanciamento_id is not None
            else Comprovante.Origem.SOLICITACAO_RENOVACAO
        ),
        "created_at": activation_at,
        "legacy_comprovante_id": None,
    }


def _comprovante_display_cycle_number(
    comprovante: Comprovante,
    *,
    contrato: Contrato,
    renewal: EffectiveRenewal | None,
) -> int | None:
    refinanciamento = getattr(comprovante, "refinanciamento", None)
    ciclo_destino = getattr(refinanciamento, "ciclo_destino", None)
    if (
        ciclo_destino is not None
        and getattr(ciclo_destino, "contrato_id", None) == contrato.id
    ):
        return ciclo_destino.numero
    if (
        renewal is not None
        and comprovante.refinanciamento_id == renewal.refinanciamento.id
        and ciclo_destino is None
    ):
        return None
    ciclo = getattr(comprovante, "ciclo", None)
    if ciclo is not None and getattr(ciclo, "contrato_id", None) == contrato.id:
        return ciclo.numero
    return None


def _cycle_documents(
    contrato: Contrato,
    *,
    cycle_number: int,
    renewal: EffectiveRenewal | None,
    include_documents: bool,
    visible_cycle_numbers: set[int] | None = None,
) -> tuple[list[dict[str, object]], dict[str, object] | None]:
    if not include_documents:
        return [], None

    queryset = (
        Comprovante.objects.filter(deleted_at__isnull=True)
        .filter(
            Q(contrato=contrato)
            | Q(ciclo__contrato=contrato)
            | Q(refinanciamento__contrato_origem=contrato)
        )
        .select_related(
            "enviado_por",
            "refinanciamento",
            "refinanciamento__ciclo_destino",
            "ciclo",
        )
        .order_by("created_at", "id")
    )
    comprovantes_map: dict[int, Comprovante] = {}
    for comprovante in queryset:
        display_cycle_number = _comprovante_display_cycle_number(
            comprovante,
            contrato=contrato,
            renewal=renewal,
        )
        fallback_to_latest_visible = (
            display_cycle_number is not None
            and visible_cycle_numbers is not None
            and display_cycle_number not in visible_cycle_numbers
            and cycle_number == max(visible_cycle_numbers)
        )
        if display_cycle_number == cycle_number or (
            display_cycle_number is None
            and renewal is not None
            and comprovante.refinanciamento_id == renewal.refinanciamento.id
        ) or fallback_to_latest_visible:
            comprovantes_map[comprovante.id] = comprovante

    comprovantes = sorted(
        comprovantes_map.values(),
        key=lambda item: (item.created_at, item.id),
    )

    termos = [
        comprovante
        for comprovante in comprovantes
        if comprovante.tipo == Comprovante.Tipo.TERMO_ANTECIPACAO
    ]
    primary_termo = (
        sorted(
            termos,
            key=lambda item: (
                item.papel == Comprovante.Papel.OPERACIONAL,
                item.created_at,
                item.id,
            ),
        )[-1]
        if termos
        else None
    )
    termo = _serialize_comprovante(primary_termo) if primary_termo is not None else None
    comprovantes_ciclo = [
        _serialize_comprovante(comprovante)
        for comprovante in comprovantes
        if comprovante.tipo != Comprovante.Tipo.TERMO_ANTECIPACAO
        or comprovante.id != getattr(primary_termo, "id", None)
    ]
    if termo is None and renewal is not None:
        termo = _synthesize_termo_payload(renewal.refinanciamento)
    return comprovantes_ciclo, termo


def _build_projection_parcela(
    *,
    contrato: Contrato,
    cycle_number: int,
    slot_number: int,
    referencia_mes: date,
    status: str,
    data_pagamento: date | None = None,
    observacao: str = "",
    valor: Decimal | None = None,
) -> dict[str, object]:
    return {
        "id": _projection_id(
            contrato_id=contrato.id,
            cycle_number=cycle_number,
            slot=slot_number,
        ),
        "numero": slot_number,
        "referencia_mes": referencia_mes,
        "valor": valor or contrato.valor_mensalidade,
        "data_vencimento": referencia_mes,
        "status": status,
        "data_pagamento": data_pagamento,
        "observacao": observacao,
    }


def _coerce_regularized_renewal_reference(item: FinancialReference) -> FinancialReference:
    return FinancialReference(
        referencia_mes=item.referencia_mes,
        status=PROJECTION_STATUS_QUITADA,
        data_pagamento=item.data_pagamento,
        valor=item.valor,
        observacao=(
            item.observacao
            or "Competência quitada via relatório manual na ativação do ciclo."
        ),
        source=item.source,
        counts_for_cycle=True,
        had_unpaid_event=item.had_unpaid_event,
    )


def _build_cycle_dict(
    *,
    contrato: Contrato,
    cycle_number: int,
    referencias: list[date],
    parcelas: list[dict[str, object]],
    status: str,
    phase_slug: str,
    financial_slug: str,
    activation_at: datetime | None,
    activation_source: str,
    activation_inferred: bool,
    data_solicitacao_renovacao: datetime | None,
    renewal: EffectiveRenewal | None,
    refinanciamento_operacional: Refinanciamento | None,
    include_documents: bool,
    visible_cycle_numbers: set[int] | None,
) -> dict[str, object]:
    cycle_size = len(parcelas)
    valor_total = (contrato.valor_mensalidade * Decimal(str(cycle_size))).quantize(
        Decimal("0.01")
    )
    comprovantes_ciclo, termo_antecipacao = _cycle_documents(
        contrato,
        cycle_number=cycle_number,
        renewal=renewal,
        include_documents=include_documents,
        visible_cycle_numbers=visible_cycle_numbers,
    )
    refinement = renewal.refinanciamento if renewal is not None else refinanciamento_operacional
    visual_status = _compose_visual_status(phase_slug=phase_slug)
    return {
        "id": _projection_cycle_id(contrato_id=contrato.id, cycle_number=cycle_number),
        "contrato_id": contrato.id,
        "contrato_codigo": contrato.codigo,
        "contrato_status": contrato.status,
        "numero": cycle_number,
        "data_inicio": referencias[0],
        "data_fim": referencias[-1],
        "status": status,
        "fase_ciclo": phase_slug,
        "situacao_financeira": financial_slug,
        "status_visual_slug": visual_status["status_visual_slug"],
        "status_visual_label": visual_status["status_visual_label"],
        "valor_total": valor_total,
        "data_ativacao_ciclo": activation_at,
        "origem_data_ativacao": activation_source,
        "ativacao_inferida": activation_inferred,
        "data_solicitacao_renovacao": data_solicitacao_renovacao,
        "data_renovacao": renewal.activated_at if renewal is not None else None,
        "origem_renovacao": renewal.refinanciamento.origem if renewal is not None else "",
        "primeira_competencia_ciclo": referencias[0],
        "ultima_competencia_ciclo": referencias[-1],
        "resumo_referencias": ", ".join(
            referencia.strftime("%m/%Y") for referencia in referencias
        ),
        "refinanciamento_id": refinement.id if refinement is not None else None,
        "legacy_refinanciamento_id": (
            renewal.refinanciamento.legacy_refinanciamento_id if renewal is not None else None
        ),
        "comprovantes_ciclo": comprovantes_ciclo,
        "termo_antecipacao": termo_antecipacao,
        "parcelas": parcelas,
    }


def _build_eligible_references(
    *,
    seed_reference: date,
    cycle_count: int,
    cycle_size: int,
    blacklisted_references: set[date],
) -> list[date]:
    references: list[date] = []
    current_reference = seed_reference
    target_total = max(cycle_count, 1) * max(cycle_size, 1)
    safety_limit = target_total + len(blacklisted_references) + 36
    steps = 0

    while len(references) < target_total and steps < safety_limit:
        normalized = current_reference.replace(day=1)
        if normalized not in blacklisted_references:
            references.append(normalized)
        current_reference = _add_months(current_reference, 1)
        steps += 1

    return references


def _financial_row(item: FinancialReference, *, contrato: Contrato, index: int) -> dict[str, object]:
    return {
        "id": -((contrato.id * 10000) + index + 1),
        "contrato_id": contrato.id,
        "contrato_codigo": contrato.codigo,
        "referencia_mes": item.referencia_mes,
        "valor": item.valor,
        "status": item.status,
        "data_pagamento": item.data_pagamento,
        "observacao": item.observacao,
        "source": item.source,
    }


def _normalize_projected_cycle_rows(
    *,
    contrato: Contrato,
    cycles: list[dict[str, object]],
    unpaid_rows: list[dict[str, object]],
) -> tuple[list[dict[str, object]], list[dict[str, object]], list[dict[str, object]]]:
    if not cycles:
        unresolved = [item for item in unpaid_rows if _is_unresolved_unpaid_row(item)]
        return cycles, unpaid_rows, unresolved

    latest_cycle_number = max(int(cycle.get("numero") or 0) for cycle in cycles)
    current_month = timezone.localdate().replace(day=1)
    normalized_unpaid = list(unpaid_rows)
    seen_unpaid_refs = {
        (
            int(item.get("contrato_id") or contrato.id),
            _month_floor(item.get("referencia_mes")),
        )
        for item in normalized_unpaid
        if _month_floor(item.get("referencia_mes")) is not None
    }

    for cycle in cycles:
        cycle_status = str(cycle.get("status") or "")
        cycle_number = int(cycle.get("numero") or 0)
        kept_rows: list[dict[str, object]] = []
        for parcela in list(cycle.get("parcelas") or []):
            referencia_mes = _month_floor(parcela.get("referencia_mes"))
            status = str(parcela.get("status") or "")
            observacao = str(parcela.get("observacao") or "")
            observacao_normalizada = observacao.lower()
            forced_outside_cycle = is_forced_outside_cycle_reference(referencia_mes)

            should_move = False
            normalized_status = status
            if forced_outside_cycle:
                should_move = True
                normalized_status = (
                    PROJECTION_STATUS_QUITADA
                    if status
                    in {
                        Parcela.Status.DESCONTADO,
                        Parcela.Status.LIQUIDADA,
                        PROJECTION_STATUS_QUITADA,
                    }
                    else Parcela.Status.NAO_DESCONTADO
                )
            elif status in {Parcela.Status.NAO_DESCONTADO, PROJECTION_STATUS_QUITADA}:
                should_move = True
            elif status == Parcela.Status.EM_PREVISAO and referencia_mes is not None:
                keep_trailing_forecast = _is_current_month_trailing_forecast(
                    list(cycle.get("parcelas") or []),
                    current_month=current_month,
                )
                if (
                    referencia_mes < current_month
                    or (
                        cycle_number < latest_cycle_number
                        and not keep_trailing_forecast
                    )
                    or cycle_status == Ciclo.Status.CICLO_RENOVADO
                ):
                    should_move = True
                    normalized_status = Parcela.Status.NAO_DESCONTADO
            elif any(hint in observacao_normalizada for hint in MANUAL_OUTSIDE_CYCLE_HINTS):
                if status in {
                    Parcela.Status.DESCONTADO,
                    Parcela.Status.LIQUIDADA,
                    PROJECTION_STATUS_QUITADA,
                }:
                    should_move = True
                    normalized_status = PROJECTION_STATUS_QUITADA

            if not should_move:
                kept_rows.append(parcela)
                continue

            unpaid_row = {
                **parcela,
                "contrato_id": contrato.id,
                "contrato_codigo": contrato.codigo,
                "status": normalized_status,
                "data_pagamento": (
                    None
                    if normalized_status == Parcela.Status.NAO_DESCONTADO
                    else parcela.get("data_pagamento")
                ),
                "observacao": (
                    observacao
                    or (
                        "Competência quitada fora do ciclo."
                        if normalized_status == PROJECTION_STATUS_QUITADA
                        else "Competência vencida fora do ciclo."
                    )
                ),
            }
            dedupe_key = (contrato.id, referencia_mes)
            if referencia_mes is not None and dedupe_key in seen_unpaid_refs:
                continue
            if referencia_mes is not None:
                seen_unpaid_refs.add(dedupe_key)
            normalized_unpaid.append(unpaid_row)

        cycle["parcelas"] = [
            {
                **parcela,
                "numero": index + 1,
            }
            for index, parcela in enumerate(
                sorted(
                    kept_rows,
                    key=lambda item: (
                        _month_floor(item.get("referencia_mes")) or date.min,
                        int(item.get("id") or 0),
                    ),
                )
            )
        ]
        if cycle["parcelas"]:
            cycle["primeira_competencia_ciclo"] = cycle["parcelas"][0]["referencia_mes"]
            cycle["ultima_competencia_ciclo"] = cycle["parcelas"][-1]["referencia_mes"]
            cycle["resumo_referencias"] = ", ".join(
                parcela["referencia_mes"].strftime("%m/%Y")
                for parcela in cycle["parcelas"]
            )

    normalized_unpaid = sorted(
        normalized_unpaid,
        key=lambda item: item["referencia_mes"],
        reverse=True,
    )
    unresolved = [item for item in normalized_unpaid if _is_unresolved_unpaid_row(item)]
    return cycles, normalized_unpaid, unresolved


def _manual_phase_slug(
    *,
    contrato: Contrato,
    cycle_status: str,
    is_latest_cycle: bool,
    latest_cycle_parcelas: list[dict[str, object]] | None = None,
    block_auto_renewal: bool = False,
) -> str:
    associado_status = getattr(contrato.associado, "status", "")
    if associado_status == Associado.Status.INATIVO or contrato.status == Contrato.Status.CANCELADO:
        return "contrato_desativado"
    if contrato.status == Contrato.Status.ENCERRADO:
        return "contrato_encerrado"
    if is_latest_cycle:
        refinanciamento_operacional = _active_operational_refinanciamentos(contrato)
        if refinanciamento_operacional:
            normalized = _normalize_operational_status(refinanciamento_operacional[0].status)
            if normalized in OPERATIONAL_STATUS_TO_VISUAL_PHASE:
                return OPERATIONAL_STATUS_TO_VISUAL_PHASE[normalized]
    if cycle_status in CONCLUDED_CYCLE_STATUSES:
        return "ciclo_renovado"
    if cycle_status == Ciclo.Status.PENDENCIA:
        return "ciclo_com_pendencia"
    if (
        is_latest_cycle
        and cycle_status == Ciclo.Status.APTO_A_RENOVAR
        and has_cancelled_operational_refinanciamento(contrato)
    ):
        return "ciclo_aberto"
    if cycle_status == Ciclo.Status.APTO_A_RENOVAR:
        return "apto_a_renovar"
    return "ciclo_aberto"


def _manual_cycle_documents(
    contrato: Contrato,
    ciclo: Ciclo,
    *,
    include_documents: bool,
    visible_cycle_numbers: set[int] | None,
) -> tuple[list[dict[str, object]], dict[str, object] | None]:
    return _cycle_documents(
        contrato,
        cycle_number=ciclo.numero,
        renewal=None,
        include_documents=include_documents,
        visible_cycle_numbers=visible_cycle_numbers,
    )


def _build_manual_contract_projection(
    contrato: Contrato,
    *,
    include_documents: bool = False,
) -> dict[str, object]:
    ciclos = list(
        contrato.ciclos.filter(deleted_at__isnull=True).order_by("numero", "id")
    )
    parcelas = list(
        Parcela.all_objects.filter(ciclo__contrato=contrato, deleted_at__isnull=True)
        .exclude(status=Parcela.Status.CANCELADO)
        .select_related("ciclo")
        .order_by("ciclo__numero", "numero", "id")
    )
    parcelas_por_ciclo: dict[int, list[dict[str, object]]] = {}
    unpaid_rows: list[dict[str, object]] = []
    movement_rows: list[dict[str, object]] = []

    def build_row(
        parcela: Parcela,
    ) -> dict[str, object]:
        return {
            "id": parcela.id,
            "ciclo_id": parcela.ciclo_id,
            "numero": parcela.numero,
            "referencia_mes": parcela.referencia_mes,
            "valor": parcela.valor,
            "data_vencimento": parcela.data_vencimento,
            "status": parcela.status,
            "data_pagamento": parcela.data_pagamento,
            "observacao": parcela.observacao,
            "layout_bucket": parcela.layout_bucket,
            "source": "admin_override",
        }

    for parcela in parcelas:
        row = build_row(parcela)
        if parcela.layout_bucket == Parcela.LayoutBucket.UNPAID:
            unpaid_rows.append(
                {
                    "contrato_id": contrato.id,
                    "contrato_codigo": contrato.codigo,
                    **row,
                }
            )
            continue
        if parcela.layout_bucket == Parcela.LayoutBucket.MOVEMENT:
            movement_rows.append(
                {
                    "contrato_id": contrato.id,
                    "contrato_codigo": contrato.codigo,
                    **row,
                }
            )
            continue
        if parcela.status == Parcela.Status.NAO_DESCONTADO:
            unpaid_rows.append(
                {
                    "contrato_id": contrato.id,
                    "contrato_codigo": contrato.codigo,
                    **row,
                    "source": "admin_override_cycle",
                }
            )
        parcelas_por_ciclo.setdefault(parcela.ciclo_id, []).append(row)

    unresolved_unpaid_rows = [
        item for item in unpaid_rows if _is_unresolved_unpaid_row(item)
    ]
    has_unpaid = bool(unresolved_unpaid_rows)
    block_small_value_renewal = is_return_imported_small_value_contract(contrato)
    latest_operational_refinanciamento = _latest_operational_refinanciamento(contrato)
    refinanciamento_operacional_list = (
        []
        if block_small_value_renewal
        else _active_operational_refinanciamentos(contrato)
    )
    refinanciamento_operacional = refinanciamento_operacional_list[0] if refinanciamento_operacional_list else None
    projected_cycles: list[dict[str, object]] = []
    visible_cycles = [
        (
            ciclo,
            sorted(
                parcelas_por_ciclo.get(ciclo.id, []),
                key=lambda item: (
                    item["referencia_mes"],
                    int(item.get("numero") or 0),
                    int(item.get("id") or 0),
                ),
            ),
        )
        for ciclo in ciclos
        if parcelas_por_ciclo.get(ciclo.id)
    ]
    latest_cycle_number = max(
        (ciclo.numero for ciclo, _cycle_parcelas in visible_cycles),
        default=0,
    )
    visible_cycle_numbers = {ciclo.numero for ciclo, _cycle_parcelas in visible_cycles}

    for ciclo, cycle_parcelas in visible_cycles:
        activation = get_cycle_activation_payload(ciclo)
        is_latest_cycle = ciclo.numero == latest_cycle_number
        phase_slug = _manual_phase_slug(
            contrato=contrato,
            cycle_status=ciclo.status,
            is_latest_cycle=is_latest_cycle,
            latest_cycle_parcelas=cycle_parcelas if is_latest_cycle else None,
            block_auto_renewal=block_small_value_renewal,
        )
        financial_slug = _cycle_financial_status(
            contrato=contrato,
            has_unpaid_months=has_unpaid,
        )
        visual_status = _compose_visual_status(phase_slug=phase_slug)
        comprovantes_ciclo, termo_antecipacao = _manual_cycle_documents(
            contrato,
            ciclo,
            include_documents=include_documents,
            visible_cycle_numbers=visible_cycle_numbers,
        )
        projected_cycles.append(
            {
                "id": ciclo.id,
                "contrato_id": contrato.id,
                "contrato_codigo": contrato.codigo,
                "contrato_status": contrato.status,
                "numero": ciclo.numero,
                "data_inicio": (
                    cycle_parcelas[0]["referencia_mes"] if cycle_parcelas else ciclo.data_inicio
                ),
                "data_fim": (
                    cycle_parcelas[-1]["referencia_mes"] if cycle_parcelas else ciclo.data_fim
                ),
                "status": ciclo.status,
                "fase_ciclo": phase_slug,
                "situacao_financeira": financial_slug,
                "status_visual_slug": visual_status["status_visual_slug"],
                "status_visual_label": visual_status["status_visual_label"],
                "valor_total": ciclo.valor_total,
                "data_ativacao_ciclo": activation["data_ativacao_ciclo"],
                "origem_data_ativacao": activation["origem_data_ativacao"],
                "ativacao_inferida": activation["ativacao_inferida"],
                "data_solicitacao_renovacao": activation["data_solicitacao_renovacao"],
                "data_renovacao": None,
                "origem_renovacao": "",
                "primeira_competencia_ciclo": (
                    cycle_parcelas[0]["referencia_mes"] if cycle_parcelas else ciclo.data_inicio
                ),
                "ultima_competencia_ciclo": (
                    cycle_parcelas[-1]["referencia_mes"] if cycle_parcelas else ciclo.data_fim
                ),
                "resumo_referencias": ", ".join(
                    parcela["referencia_mes"].strftime("%m/%Y") for parcela in cycle_parcelas
                ),
                "refinanciamento_id": (
                    refinanciamento_operacional.id
                    if is_latest_cycle and refinanciamento_operacional is not None
                    else None
                ),
                "legacy_refinanciamento_id": None,
                "comprovantes_ciclo": comprovantes_ciclo,
                "termo_antecipacao": termo_antecipacao,
                "parcelas": cycle_parcelas,
            }
        )

    cancelled_current_cycle_renewal = has_cancelled_operational_refinanciamento(contrato)
    status_renovacao = ""
    refinanciamento_id: int | None = None
    if refinanciamento_operacional is not None:
        status_renovacao = _normalize_operational_status(refinanciamento_operacional.status)
        refinanciamento_id = refinanciamento_operacional.id
    elif latest_operational_refinanciamento is not None:
        status_renovacao = str(latest_operational_refinanciamento.status or "")
        refinanciamento_id = latest_operational_refinanciamento.id
    elif (
        not cancelled_current_cycle_renewal
        and projected_cycles
        and str(projected_cycles[-1].get("status") or "") == Ciclo.Status.APTO_A_RENOVAR
    ):
        status_renovacao = Refinanciamento.Status.APTO_A_RENOVAR
    elif (
        not cancelled_current_cycle_renewal
        and
        projected_cycles
        and not block_small_value_renewal
        and is_contract_eligible_for_renewal_competencia(
            contrato,
            parcelas=list(projected_cycles[-1].get("parcelas") or []),
        )
    ):
        status_renovacao = Refinanciamento.Status.APTO_A_RENOVAR

    return {
        "cycle_size": get_contract_cycle_size(contrato),
        "cycles": list(sorted(projected_cycles, key=lambda item: item["numero"], reverse=True)),
        "unpaid_months": sorted(unpaid_rows, key=lambda item: item["referencia_mes"], reverse=True),
        "possui_meses_nao_descontados": has_unpaid,
        "meses_nao_descontados_count": len(unresolved_unpaid_rows),
        "status_renovacao": status_renovacao,
        "refinanciamento_id": refinanciamento_id,
        "movimentos_financeiros_avulsos": sorted(
            movement_rows,
            key=lambda item: item["referencia_mes"],
        ),
    }


def build_contract_cycle_projection(
    contrato: Contrato,
    *,
    include_documents: bool = False,
) -> dict[str, object]:
    if contrato.admin_manual_layout_enabled:
        return _build_manual_contract_projection(
            contrato,
            include_documents=include_documents,
        )

    cycle_size = get_contract_cycle_size(contrato)
    threshold = get_future_generation_threshold(contrato)
    paid, unpaid, regularized = _merge_financial_references(contrato)
    paid_map = {item.referencia_mes: item for item in paid}
    regularized_map = {item.referencia_mes: item for item in regularized}
    all_refs = [*paid, *unpaid, *regularized]
    unpaid_reference_set = {item.referencia_mes for item in unpaid}
    regularized_reference_set = {item.referencia_mes for item in regularized}

    if not _contract_is_activated(contrato, all_refs):
        unpaid_rows = [
            _financial_row(item, contrato=contrato, index=index)
            for index, item in enumerate(
                sorted(
                    [*unpaid, *regularized],
                    key=lambda item: item.referencia_mes,
                    reverse=True,
                )
            )
        ]
        return {
            "cycle_size": cycle_size,
            "cycles": [],
            "unpaid_months": unpaid_rows,
            "possui_meses_nao_descontados": bool(unpaid),
            "meses_nao_descontados_count": len(unpaid),
            "status_renovacao": "",
            "refinanciamento_id": None,
            "movimentos_financeiros_avulsos": [
                _financial_row(item, contrato=contrato, index=index)
                for index, item in enumerate(
                    sorted(
                        [item for item in paid if item.referencia_mes not in regularized_reference_set],
                        key=lambda item: item.referencia_mes,
                    )
                )
            ],
        }

    activation_payload = get_contract_activation_payload(contrato)
    renewals = _effective_renewals(contrato, refs=all_refs)
    forced_regularized_paid_map = {
        renewal.first_reference: _coerce_regularized_renewal_reference(
            regularized_map[renewal.first_reference]
        )
        for renewal in renewals
        if renewal.first_reference in regularized_map
    }
    forced_outside_references = {
        item.referencia_mes
        for item in regularized
        if item.force_outside_cycle
    }
    blocked_references = (
        unpaid_reference_set | regularized_reference_set | forced_outside_references
    )
    block_small_value_renewal = is_return_imported_small_value_contract(contrato)
    operational_refis = _active_operational_refinanciamentos(contrato)
    operational_refi = (
        None
        if block_small_value_renewal
        else (operational_refis[0] if operational_refis else None)
    )
    latest_operational_refinanciamento = _latest_operational_refinanciamento(contrato)

    cycle_count = 1 + len(renewals)
    visible_cycle_numbers = set(range(1, cycle_count + 1))
    current_month = timezone.localdate().replace(day=1)
    seed_reference = _seed_reference(contrato, paid or unpaid or regularized, renewals)
    eligible_references = _build_eligible_references(
        seed_reference=seed_reference,
        cycle_count=cycle_count,
        cycle_size=cycle_size,
        blacklisted_references=blocked_references,
    )

    cycles: list[dict[str, object]] = []
    cycle_reference_set: set[date] = set()

    for index in range(1, cycle_count + 1):
        slice_start = (index - 1) * cycle_size
        references = eligible_references[slice_start : slice_start + cycle_size]
        if len(references) < cycle_size and references:
            current_reference = _add_months(references[-1], 1)
            while len(references) < cycle_size:
                if current_reference not in blocked_references:
                    references.append(current_reference)
                current_reference = _add_months(current_reference, 1)

        renewal = renewals[index - 2] if index > 1 and index - 2 < len(renewals) else None
        next_renewal = renewals[index - 1] if index - 1 < len(renewals) else None
        cycle_paid_map = paid_map
        if renewal is not None and renewal.first_reference in forced_regularized_paid_map:
            references = [
                _add_months(renewal.first_reference, offset)
                for offset in range(cycle_size)
            ]
            cycle_paid_map = {
                **paid_map,
                renewal.first_reference: forced_regularized_paid_map[
                    renewal.first_reference
                ],
            }
        cycle_reference_set.update(references)

        parcelas = []
        paid_count = 0
        seed_activated_cycle = (
            index == 1
            and not paid_map
            and not unpaid
            and not regularized
            and _contract_is_activated(contrato, all_refs)
        )
        for slot, referencia in enumerate(references, start=1):
            explicit = cycle_paid_map.get(referencia)
            if explicit is not None:
                if explicit.counts_for_cycle:
                    paid_count += 1
                parcelas.append(
                    _build_projection_parcela(
                        contrato=contrato,
                        cycle_number=index,
                        slot_number=slot,
                        referencia_mes=referencia,
                        status=explicit.status,
                        data_pagamento=explicit.data_pagamento,
                        observacao=explicit.observacao,
                        valor=explicit.valor,
                    )
                )
                continue

            default_status = Parcela.Status.EM_PREVISAO
            if seed_activated_cycle:
                default_status = (
                    Parcela.Status.EM_ABERTO if slot == 1 else Parcela.Status.FUTURO
                )
            parcelas.append(
                _build_projection_parcela(
                    contrato=contrato,
                    cycle_number=index,
                    slot_number=slot,
                    referencia_mes=referencia,
                    status=default_status,
                )
            )

        if next_renewal is not None:
            cycle_status = _renewed_cycle_status(
                parcelas,
                current_month=current_month,
                cycle_size=cycle_size,
            )
        elif paid_count >= threshold:
            cycle_status = (
                blocked_small_value_cycle_status(has_unpaid_months=bool(unpaid))
                if block_small_value_renewal and index == cycle_count
                else Ciclo.Status.APTO_A_RENOVAR
            )
        else:
            cycle_status = Ciclo.Status.ABERTO

        financial_slug = _cycle_financial_status(
            contrato=contrato,
            has_unpaid_months=bool(unpaid),
        )
        phase_slug = _cycle_phase_status(
            contrato=contrato,
            cycle_status=cycle_status,
            next_renewal=next_renewal,
            refinanciamento_operacional=(
                operational_refi if index == cycle_count else None
            ),
        )

        if index == 1:
            activation_at = activation_payload["data_primeiro_ciclo_ativado"]
            activation_source = str(activation_payload["origem_data_primeiro_ciclo"])
            activation_inferred = bool(
                activation_payload["primeiro_ciclo_ativacao_inferida"]
            )
            request_at = (
                next_renewal.activated_at
                if next_renewal
                else (
                    operational_refi.data_ativacao_ciclo
                    or operational_refi.executado_em
                    or operational_refi.created_at
                    if operational_refi
                    else None
                )
            )
        else:
            activation_at = renewal.activated_at if renewal is not None else None
            activation_source = (
                renewal.refinanciamento.origem if renewal is not None else "indisponivel"
            )
            activation_inferred = False
            request_at = renewal.activated_at if renewal is not None else None

        cycles.append(
            _build_cycle_dict(
                contrato=contrato,
                cycle_number=index,
                referencias=references,
                parcelas=parcelas,
                status=cycle_status,
                phase_slug=phase_slug,
                financial_slug=financial_slug,
                activation_at=activation_at,
                activation_source=activation_source,
                activation_inferred=activation_inferred,
                data_solicitacao_renovacao=request_at,
                renewal=renewal,
                refinanciamento_operacional=(
                    operational_refi if index == cycle_count else None
                ),
                include_documents=include_documents,
                visible_cycle_numbers=visible_cycle_numbers,
            )
        )

    unpaid_rows = [
        _financial_row(item, contrato=contrato, index=index)
        for index, item in enumerate(
            sorted(
                [*unpaid, *regularized],
                key=lambda item: item.referencia_mes,
                reverse=True,
            )
        )
    ]
    cycles, unpaid_rows, unresolved_unpaid_rows = _normalize_projected_cycle_rows(
        contrato=contrato,
        cycles=cycles,
        unpaid_rows=unpaid_rows,
    )
    movimentos_avulsos = [
        _financial_row(item, contrato=contrato, index=index)
        for index, item in enumerate(
            sorted(
                [
                    item
                    for item in paid
                    if item.referencia_mes not in cycle_reference_set
                ],
                key=lambda item: item.referencia_mes,
            )
        )
    ]

    cancelled_current_cycle_renewal = has_cancelled_operational_refinanciamento(contrato)
    status_renovacao = ""
    refinanciamento_id: int | None = None
    if operational_refi is not None:
        status_renovacao = _normalize_operational_status(operational_refi.status)
        refinanciamento_id = operational_refi.id
    elif latest_operational_refinanciamento is not None:
        status_renovacao = str(latest_operational_refinanciamento.status or "")
        refinanciamento_id = latest_operational_refinanciamento.id
    elif (
        not cancelled_current_cycle_renewal
        and cycles
        and str(cycles[-1].get("status") or "") == Ciclo.Status.APTO_A_RENOVAR
    ):
        status_renovacao = Refinanciamento.Status.APTO_A_RENOVAR
    elif not cancelled_current_cycle_renewal and not block_small_value_renewal and cycles and sum(
        1
        for parcela in cycles[-1]["parcelas"]
        if _is_paid_cycle_status(str(parcela.get("status") or ""))
    ) >= threshold and is_contract_eligible_for_renewal_competencia(
        contrato,
        parcelas=list(cycles[-1].get("parcelas") or []),
    ):
        status_renovacao = Refinanciamento.Status.APTO_A_RENOVAR

    return {
        "cycle_size": cycle_size,
        "cycles": list(sorted(cycles, key=lambda item: item["numero"], reverse=True)),
        "unpaid_months": unpaid_rows,
        "possui_meses_nao_descontados": bool(unresolved_unpaid_rows),
        "meses_nao_descontados_count": len(unresolved_unpaid_rows),
        "status_renovacao": status_renovacao,
        "refinanciamento_id": refinanciamento_id,
        "movimentos_financeiros_avulsos": movimentos_avulsos,
    }


def get_contract_visual_status_payload(
    contrato: Contrato,
    *,
    projection: dict[str, object] | None = None,
) -> dict[str, object]:
    projection = projection or build_contract_cycle_projection(contrato)
    cycles = list(projection.get("cycles", []))

    if not cycles:
        if (
            contrato.status == Contrato.Status.ATIVO
            and contrato.auxilio_liberado_em is not None
        ):
            return {
                **_compose_visual_status(phase_slug="ciclo_aberto"),
                "possui_meses_nao_descontados": False,
                "meses_nao_descontados_count": 0,
                "cycle_id": None,
                "cycle_number": None,
            }
        return _fallback_visual_status_from_models(
            associado_status=getattr(contrato.associado, "status", ""),
            contrato_status=contrato.status,
        )

    relevant_cycle = max(cycles, key=_cycle_visual_rank)
    return {
        "status_visual_slug": relevant_cycle["status_visual_slug"],
        "status_visual_label": relevant_cycle["status_visual_label"],
        "fase_ciclo": relevant_cycle["fase_ciclo"],
        "situacao_financeira": relevant_cycle["situacao_financeira"],
        "possui_meses_nao_descontados": bool(
            projection.get("possui_meses_nao_descontados")
        ),
        "meses_nao_descontados_count": int(
            projection.get("meses_nao_descontados_count") or 0
        ),
        "cycle_id": relevant_cycle["id"],
        "cycle_number": relevant_cycle["numero"],
    }


def get_associado_visual_status_payload(associado: Associado) -> dict[str, object]:
    mother_status = resolve_associado_mother_status(associado)
    if mother_status == Associado.Status.INATIVO:
        return _compose_visual_status(phase_slug="contrato_desativado")
    if mother_status == Associado.Status.INADIMPLENTE:
        return _compose_visual_status(phase_slug="inadimplente")
    if mother_status == "apto_a_renovar":
        return _compose_visual_status(phase_slug="apto_a_renovar")

    contratos = get_operational_contracts_for_associado(associado)
    candidates: list[dict[str, object]] = []

    for contrato in contratos:
        projection = build_contract_cycle_projection(contrato)
        status_payload = get_contract_visual_status_payload(
            contrato,
            projection=projection,
        )
        phase_priority = STATUS_VISUAL_PHASE_PRIORITY.get(
            str(status_payload.get("fase_ciclo") or ""),
            -1,
        )

        candidates.append(
            {
                **status_payload,
                "phase_priority": phase_priority,
                "created_at": contrato.created_at,
            }
        )

    active_candidates = [item for item in candidates if item["phase_priority"] >= 0]
    if active_candidates:
        selected = max(
            active_candidates,
            key=lambda item: (
                int(item["phase_priority"]),
                int(item.get("meses_nao_descontados_count") or 0),
                item["created_at"],
            ),
        )
        # Associado genuinamente ativo com todos os ciclos concluídos: exibe "Ativo"
        # em vez de "Concluído" para não confundir com encerramento.
        if (
            mother_status == Associado.Status.ATIVO
            and selected["status_visual_slug"] == "ciclo_renovado"
        ):
            return _compose_visual_status(phase_slug="ciclo_aberto")
        return {
            "status_visual_slug": selected["status_visual_slug"],
            "status_visual_label": selected["status_visual_label"],
        }

    if candidates:
        selected = max(candidates, key=lambda item: item["created_at"])
        return {
            "status_visual_slug": selected["status_visual_slug"],
            "status_visual_label": selected["status_visual_label"],
        }

    return _fallback_visual_status_from_models(
        associado_status=associado.status,
        contrato_status=Contrato.Status.EM_ANALISE,
    )
