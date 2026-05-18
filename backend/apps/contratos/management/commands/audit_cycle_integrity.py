from __future__ import annotations

import csv
import json
from collections import Counter
from collections import defaultdict
from datetime import date
from pathlib import Path

from django.core.management.base import BaseCommand

from apps.associados.admin_override_service import AdminOverrideService
from apps.contratos.canonicalization import operational_contracts_queryset
from apps.contratos.cycle_projection import (
    build_contract_cycle_projection,
    resolve_current_renewal_competencia,
)
from apps.contratos.cycle_rebuild import rebuild_contract_cycle_state
from apps.contratos.cycle_timeline import get_contract_cycle_size
from apps.contratos.models import Ciclo, Contrato, Parcela
from apps.contratos.renovacao import RenovacaoCicloService
from apps.esteira.models import EsteiraItem


def _normalize_document(value: str | None) -> str:
    return "".join(char for char in str(value or "") if char.isdigit())


def _projection_signature(projection: dict[str, object]) -> dict[str, object]:
    return {
        "cycles": [
            {
                "numero": int(cycle.get("numero") or 0),
                "status": str(cycle.get("status") or ""),
                "fase_ciclo": str(cycle.get("fase_ciclo") or ""),
                "parcelas": [
                    {
                        "referencia_mes": parcela["referencia_mes"].isoformat()
                        if parcela.get("referencia_mes")
                        else None,
                        "status": str(parcela.get("status") or ""),
                        "layout_bucket": str(parcela.get("layout_bucket") or "cycle"),
                    }
                    for parcela in list(cycle.get("parcelas") or [])
                ],
            }
            for cycle in sorted(
                projection.get("cycles") or [],
                key=lambda item: int(item.get("numero") or 0),
            )
        ],
        "unpaid_months": [
            {
                "referencia_mes": row["referencia_mes"].isoformat()
                if row.get("referencia_mes")
                else None,
                "status": str(row.get("status") or ""),
            }
            for row in sorted(
                projection.get("unpaid_months") or [],
                key=lambda item: item.get("referencia_mes") or date.min,
            )
        ],
        "movimentos_financeiros_avulsos": [
            {
                "referencia_mes": row["referencia_mes"].isoformat()
                if row.get("referencia_mes")
                else None,
                "status": str(row.get("status") or ""),
            }
            for row in sorted(
                projection.get("movimentos_financeiros_avulsos") or [],
                key=lambda item: item.get("referencia_mes") or date.min,
            )
        ],
        "status_renovacao": str(projection.get("status_renovacao") or ""),
    }


class Command(BaseCommand):
    help = (
        "Audita divergências de ciclos, editor manual, fila apta e "
        "inconsistências de esteira/conclusão."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--cpf",
            action="append",
            default=[],
            help="CPF específico para auditar. Pode ser informado múltiplas vezes.",
        )
        parser.add_argument(
            "--competencia",
            type=str,
            default=None,
            help="Competência no formato YYYY-MM. Padrão: competência atual de renovação.",
        )
        parser.add_argument(
            "--fix-esteira-conclusion",
            action="store_true",
            help=(
                "Corrige inconsistências de concluído na esteira: limpa concluido_em "
                "fora da etapa concluído e preenche ausências na etapa concluído."
            ),
        )
        parser.add_argument(
            "--fix-cycle-size-violations",
            action="store_true",
            help="Reclassifica parcelas excedentes em ciclos de contratos com ciclo de 3 parcelas.",
        )
        parser.add_argument(
            "--report-json",
            default=None,
            help="Caminho opcional para exportar o relatório completo em JSON.",
        )
        parser.add_argument(
            "--report-csv",
            default=None,
            help="Caminho opcional para exportar o relatório resumido em CSV.",
        )

    def handle(self, *args, **options):
        competencia = self._parse_competencia(options.get("competencia"))
        fix_esteira_conclusion = bool(options.get("fix_esteira_conclusion"))
        fix_cycle_size_violations = bool(options.get("fix_cycle_size_violations"))
        cpfs = {_normalize_document(value) for value in options.get("cpf") or [] if value}

        contratos = list(
            operational_contracts_queryset(
                Contrato.objects.select_related("associado", "associado__esteira_item")
                .prefetch_related("ciclos__parcelas")
                .order_by("associado__nome_completo", "id")
            )
        )
        if cpfs:
            contratos = [
                contrato
                for contrato in contratos
                if _normalize_document(contrato.associado.cpf_cnpj) in cpfs
            ]

        apt_rows = RenovacaoCicloService.listar_detalhes(
            competencia=competencia,
            status="apto_a_renovar",
        )
        apt_contract_ids = {
            int(row["contrato_referencia_renovacao_id"])
            for row in apt_rows
            if row.get("contrato_referencia_renovacao_id") is not None
        }

        categories: dict[str, list[dict[str, object]]] = defaultdict(list)
        audited_associado_ids: set[int] = set()

        for contrato in contratos:
            audited_associado_ids.add(contrato.associado_id)
            projection = build_contract_cycle_projection(contrato)
            editor_projection = AdminOverrideService.build_contract_projection_for_response(
                contrato,
                include_documents=False,
            )
            base_row = {
                "contrato_id": contrato.id,
                "contrato_codigo": contrato.codigo,
                "associado_id": contrato.associado_id,
                "cpf_cnpj": contrato.associado.cpf_cnpj,
                "nome": contrato.associado.nome_completo,
            }

            if contrato.admin_manual_layout_enabled and _projection_signature(
                projection
            ) != _projection_signature(editor_projection):
                categories["manual_canonical_divergence"].append(base_row)

            if (
                str(projection.get("status_renovacao") or "") == "apto_a_renovar"
                and contrato.id not in apt_contract_ids
            ):
                categories["apto_without_queue_row"].append(base_row)

            parcelas = list(
                Parcela.all_objects.filter(
                    ciclo__contrato=contrato,
                    deleted_at__isnull=True,
                )
                .exclude(status=Parcela.Status.CANCELADO)
                .select_related("ciclo")
                .order_by("referencia_mes", "id")
            )
            active_by_reference: defaultdict[date, list[Parcela]] = defaultdict(list)
            for parcela in parcelas:
                active_by_reference[parcela.referencia_mes].append(parcela)
            duplicate_refs = [
                referencia.isoformat()
                for referencia, rows in active_by_reference.items()
                if len(rows) > 1
            ]
            if duplicate_refs:
                categories["duplicate_reference_active"].append(
                    {
                        **base_row,
                        "referencias": duplicate_refs,
                    }
                )

            visible_cycle_ids = {
                parcela.ciclo_id
                for parcela in parcelas
                if parcela.layout_bucket == Parcela.LayoutBucket.CYCLE
            }
            active_cycle_ids = set(
                contrato.ciclos.filter(deleted_at__isnull=True).values_list("id", flat=True)
            )
            if active_cycle_ids and not visible_cycle_ids and any(
                parcela.layout_bucket != Parcela.LayoutBucket.CYCLE for parcela in parcelas
            ):
                categories["empty_anchor_cycle"].append(base_row)

            cycle_size_violations = self._collect_cycle_size_violations(
                contrato=contrato,
                parcelas=parcelas,
            )
            if cycle_size_violations:
                categories["cycle_size_violation"].extend(
                    [{**base_row, **row} for row in cycle_size_violations]
                )
                if fix_cycle_size_violations:
                    repaired = self._repair_cycle_size_violations(contrato)
                    if repaired:
                        categories["fixed_cycle_size_violation"].append(
                            {
                                **base_row,
                                "reclassified": repaired,
                            }
                        )

        esteiras = {
            item.associado_id: item
            for item in EsteiraItem.objects.filter(associado_id__in=audited_associado_ids)
        }
        for associado_id in sorted(audited_associado_ids):
            esteira = esteiras.get(associado_id)
            if esteira is None:
                continue
            inconsistent = (
                esteira.etapa_atual != EsteiraItem.Etapa.CONCLUIDO
                and esteira.concluido_em is not None
            ) or (
                esteira.etapa_atual == EsteiraItem.Etapa.CONCLUIDO
                and esteira.concluido_em is None
            )
            if not inconsistent:
                continue
            if fix_esteira_conclusion:
                if esteira.etapa_atual != EsteiraItem.Etapa.CONCLUIDO and esteira.concluido_em is not None:
                    esteira.concluido_em = None
                    esteira.save(update_fields=["concluido_em", "updated_at"])
                    categories["fixed_esteira_conclusion_inconsistency"].append(
                        {
                            "associado_id": associado_id,
                            "cpf_cnpj": esteira.associado.cpf_cnpj,
                            "nome": esteira.associado.nome_completo,
                            "action": "cleared_stale_concluido_em",
                        }
                    )
                    continue
                if esteira.etapa_atual == EsteiraItem.Etapa.CONCLUIDO and esteira.concluido_em is None:
                    esteira.concluido_em = esteira.updated_at or esteira.created_at
                    esteira.save(update_fields=["concluido_em", "updated_at"])
                    categories["fixed_esteira_conclusion_inconsistency"].append(
                        {
                            "associado_id": associado_id,
                            "cpf_cnpj": esteira.associado.cpf_cnpj,
                            "nome": esteira.associado.nome_completo,
                            "action": "backfilled_missing_concluido_em",
                        }
                    )
                    continue
            categories["esteira_conclusion_inconsistency"].append(
                {
                    "associado_id": associado_id,
                    "cpf_cnpj": esteira.associado.cpf_cnpj,
                    "nome": esteira.associado.nome_completo,
                    "etapa_atual": esteira.etapa_atual,
                    "concluido_em": (
                        esteira.concluido_em.isoformat() if esteira.concluido_em else None
                    ),
                }
            )

        self.stdout.write(f"Competência auditada: {competencia:%Y-%m}")
        self.stdout.write(f"Contratos auditados: {len(contratos)}")
        for category in [
            "manual_canonical_divergence",
            "apto_without_queue_row",
            "duplicate_reference_active",
            "empty_anchor_cycle",
            "cycle_size_violation",
            "fixed_cycle_size_violation",
            "esteira_conclusion_inconsistency",
            "fixed_esteira_conclusion_inconsistency",
        ]:
            rows = categories.get(category, [])
            self.stdout.write(f"{category}: {len(rows)}")
            for row in rows[:50]:
                self.stdout.write(f"- {row}")
        self._write_reports(
            categories=categories,
            report_json=options.get("report_json"),
            report_csv=options.get("report_csv"),
        )

    def _parse_competencia(self, raw_value: str | None) -> date:
        if raw_value:
            return date.fromisoformat(f"{raw_value}-01")
        return resolve_current_renewal_competencia()

    def _collect_cycle_size_violations(
        self,
        *,
        contrato: Contrato,
        parcelas: list[Parcela],
    ) -> list[dict[str, object]]:
        cycle_size = get_contract_cycle_size(contrato)
        by_cycle: defaultdict[int, list[Parcela]] = defaultdict(list)
        for parcela in parcelas:
            if parcela.layout_bucket == Parcela.LayoutBucket.CYCLE:
                by_cycle[parcela.ciclo_id].append(parcela)

        violations: list[dict[str, object]] = []
        for ciclo in contrato.ciclos.filter(deleted_at__isnull=True).order_by("numero", "id"):
            cycle_rows = sorted(
                by_cycle.get(ciclo.id, []),
                key=lambda item: (item.referencia_mes, item.numero, item.id),
            )
            if len(cycle_rows) <= cycle_size:
                continue
            status_counter = Counter(str(parcela.status or "") for parcela in cycle_rows)
            violations.append(
                {
                    "cycle_id": ciclo.id,
                    "cycle_number": ciclo.numero,
                    "cycle_size": cycle_size,
                    "total_cycle_parcelas": len(cycle_rows),
                    "status_pattern": ", ".join(
                        f"{status}={count}"
                        for status, count in sorted(status_counter.items())
                    ),
                    "parcelas": [
                        {
                            "id": parcela.id,
                            "referencia_mes": parcela.referencia_mes.isoformat(),
                            "status": parcela.status,
                            "layout_bucket": parcela.layout_bucket,
                        }
                        for parcela in cycle_rows
                    ],
                }
            )
        return violations

    def _repair_cycle_size_violations(self, contrato: Contrato) -> int:
        cycle_size = get_contract_cycle_size(contrato)
        changed = 0
        active_cycles = list(
            contrato.ciclos.filter(deleted_at__isnull=True).order_by("numero", "id")
        )
        next_cycle_by_number = {
            cycle.numero: active_cycles[index + 1] if index + 1 < len(active_cycles) else None
            for index, cycle in enumerate(active_cycles)
        }
        resolved_statuses = {
            Parcela.Status.DESCONTADO,
            Parcela.Status.LIQUIDADA,
            "quitada",
        }

        for ciclo in active_cycles:
            cycle_rows = list(
                Parcela.all_objects.filter(
                    ciclo=ciclo,
                    deleted_at__isnull=True,
                    layout_bucket=Parcela.LayoutBucket.CYCLE,
                )
                .exclude(status=Parcela.Status.CANCELADO)
                .order_by("referencia_mes", "numero", "id")
            )
            if len(cycle_rows) <= cycle_size:
                continue
            extras = cycle_rows[cycle_size:]
            next_cycle = next_cycle_by_number.get(ciclo.numero)
            next_cycle_count = (
                Parcela.all_objects.filter(
                    ciclo=next_cycle,
                    deleted_at__isnull=True,
                    layout_bucket=Parcela.LayoutBucket.CYCLE,
                )
                .exclude(status=Parcela.Status.CANCELADO)
                .count()
                if next_cycle is not None
                else 0
            )
            for parcela in extras:
                status = str(parcela.status or "")
                if status in resolved_statuses:
                    if parcela.layout_bucket != Parcela.LayoutBucket.MOVEMENT:
                        parcela.layout_bucket = Parcela.LayoutBucket.MOVEMENT
                        parcela.save(update_fields=["layout_bucket", "updated_at"])
                        changed += 1
                    continue
                if status == Parcela.Status.NAO_DESCONTADO:
                    if parcela.layout_bucket != Parcela.LayoutBucket.UNPAID:
                        parcela.layout_bucket = Parcela.LayoutBucket.UNPAID
                        parcela.save(update_fields=["layout_bucket", "updated_at"])
                        changed += 1
                    continue
                if next_cycle is not None and next_cycle_count < cycle_size:
                    parcela.ciclo = next_cycle
                    parcela.layout_bucket = Parcela.LayoutBucket.CYCLE
                    parcela.save(update_fields=["ciclo", "layout_bucket", "updated_at"])
                    next_cycle_count += 1
                    changed += 1
                    continue
                parcela.soft_delete()
                changed += 1

        if changed:
            rebuild_contract_cycle_state(contrato, execute=True)
        return changed

    def _write_reports(
        self,
        *,
        categories: dict[str, list[dict[str, object]]],
        report_json: str | None,
        report_csv: str | None,
    ) -> None:
        if report_json:
            target = Path(report_json).expanduser()
        elif report_csv:
            target = None
        else:
            target = None
        if target is not None:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                json.dumps(categories, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            self.stdout.write(f"Relatório JSON: {target}")

        if report_csv:
            target = Path(report_csv).expanduser()
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=["category", "payload"])
                writer.writeheader()
                for category, rows in categories.items():
                    for row in rows:
                        writer.writerow(
                            {
                                "category": category,
                                "payload": json.dumps(row, ensure_ascii=False),
                            }
                        )
            self.stdout.write(f"Relatório CSV: {target}")
