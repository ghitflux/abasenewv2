"use client";

import * as React from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  ArrowLeftRightIcon,
  CircleAlertIcon,
  CircleHelpIcon,
  PaperclipIcon,
  PlusIcon,
  Trash2Icon,
} from "lucide-react";
import { toast } from "sonner";

import type {
  AdminAssociadoEditorPayload,
  AdminEditorContrato,
  AdminEditorCiclo,
  AdminEditorParcela,
  ComprovanteCiclo,
} from "@/lib/api/types";
import { apiFetch } from "@/lib/api/client";
import { buildBackendFileUrl } from "@/lib/backend-files";
import {
  centsToDecimal,
  decimalToCents,
  formatMonthYear,
} from "@/lib/formatters";
import { cn } from "@/lib/utils";
import { parseIsoDate } from "@/components/associados/contrato-dates";
import CalendarCompetencia from "@/components/custom/calendar-competencia";
import DatePicker from "@/components/custom/date-picker";
import FileUploadDropzone from "@/components/custom/file-upload-dropzone";
import InputCurrency from "@/components/custom/input-currency";
import StatusBadge from "@/components/custom/status-badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";

const contratoStatusOptions = [
  "rascunho",
  "em_analise",
  "ativo",
  "encerrado",
  "cancelado",
];

const cicloStatusOptions = [
  { value: "futuro", label: "Futuro" },
  { value: "aberto", label: "Aberto" },
  { value: "pendencia", label: "Pendência" },
  { value: "ciclo_renovado", label: "Ciclo já renovado" },
  { value: "apto_a_renovar", label: "Apto a renovar" },
  { value: "concluido", label: "Concluído" },
] as const;

const parcelaStatusOptions = [
  "futuro",
  "em_aberto",
  "em_previsao",
  "descontado",
  "liquidada",
  "nao_descontado",
];

type ContractDraft = AdminEditorContrato & {
  ciclos: Array<AdminEditorCiclo & { client_key?: string }>;
};

type Props = {
  associadoId: number;
  contract: AdminEditorContrato;
  onPayloadRefresh: (
    payload?: AdminAssociadoEditorPayload,
  ) => Promise<void> | void;
  onDirtyChange?: (state: ContractEditorDirtyState) => void;
  renewalStageOptions?: ReadonlyArray<{ value: string; label: string }>;
  selectedRenewalStage?: string;
  onSelectedRenewalStageChange?: (value: string) => void;
  onRequestRenewalStageTransition?: () => void;
  renewalTransitionDisabled?: boolean;
  renewalTransitionPending?: boolean;
};

export type SaveAllContratoCorePayload = {
  updated_at: string | null;
  status: string;
  valor_bruto: string;
  valor_liquido: string;
  valor_mensalidade: string;
  prazo_meses: number;
  taxa_antecipacao: string;
  margem_disponivel: string;
  valor_total_antecipacao: string;
  doacao_associado: string;
  comissao_agente: string;
  data_contrato: string | null;
  data_aprovacao: string | null;
  data_primeira_mensalidade: string | null;
  mes_averbacao: string | null;
  auxilio_liberado_em: string | null;
};

export type SaveAllCyclesPayload = {
  updated_at: string | null;
  cycles: Array<{
    id: number | null;
    client_key?: string;
    numero: number;
    data_inicio: string;
    data_fim: string;
    status: string;
    valor_total: string;
  }>;
  parcelas: Array<{
    id: number | null;
    cycle_ref: string;
    numero: number;
    referencia_mes: string;
    valor: string;
    data_vencimento: string;
    status: string;
    data_pagamento: string | null;
    observacao: string;
    layout_bucket: string;
  }>;
};

export type SaveAllRefinanciamentoPayload = {
  id: number;
  updated_at: string | null;
  valor_refinanciamento: string;
  repasse_agente: string;
  competencia_solicitada: string;
  observacao: string;
  analista_note: string;
  coordenador_note: string;
};

export type ContractEditorDirtyState = {
  core: boolean;
  cycles: boolean;
  refinanciamento: boolean;
};

export type AdminContractEditorPendingChanges = {
  id: number;
  dirty_sections?: Array<"contract_core" | "cycle_layout" | "refinanciamento">;
  core?: SaveAllContratoCorePayload;
  cycles?: SaveAllCyclesPayload;
  refinanciamento?: SaveAllRefinanciamentoPayload;
};

export type AdminContractEditorHandle = {
  getPendingChanges: () => AdminContractEditorPendingChanges | null;
  hasPendingChanges: () => boolean;
};

function monthToReference(value: string) {
  return value ? `${value}-01` : "";
}

function nextCycleNumber(contract: ContractDraft) {
  return (
    contract.ciclos.reduce((max, item) => Math.max(max, item.numero), 0) + 1
  );
}

function buildBlankParcel(
  numero: number,
  referencia?: string,
): AdminEditorParcela {
  const month = referencia || new Date().toISOString().slice(0, 7);
  const full = monthToReference(month);
  return {
    id: null,
    numero,
    referencia_mes: full,
    valor: "0.00",
    data_vencimento: full,
    status: "em_previsao",
    data_pagamento: null,
    observacao: "",
    layout_bucket: "cycle",
    updated_at: null,
    financial_flags: {
      tem_retorno: false,
      tem_baixa_manual: false,
      tem_liquidacao: false,
    },
  };
}

function isoDate(value?: Date) {
  if (!value) {
    return null;
  }
  const year = value.getFullYear();
  const month = String(value.getMonth() + 1).padStart(2, "0");
  const day = String(value.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function monthDate(value?: string | null) {
  const parsed = parseIsoDate(value);
  return parsed
    ? new Date(parsed.getFullYear(), parsed.getMonth(), 1)
    : undefined;
}

function monthOrder(value?: string | null) {
  const parsed = monthDate(value);
  if (!parsed) {
    return null;
  }
  return parsed.getFullYear() * 12 + parsed.getMonth();
}

function openFile(url?: string | null) {
  if (!url) {
    return;
  }
  window.open(buildBackendFileUrl(url), "_blank", "noopener,noreferrer");
}

function AdminLabel({ label, tooltip }: { label: string; tooltip: string }) {
  return (
    <div className="flex items-center gap-2">
      <p className="text-xs uppercase tracking-[0.16em] text-muted-foreground">
        {label}
      </p>
      <Tooltip>
        <TooltipTrigger asChild>
          <button
            type="button"
            className="inline-flex size-4 items-center justify-center rounded-full text-muted-foreground transition hover:text-foreground"
            aria-label={`Ajuda sobre ${label}`}
          >
            <CircleHelpIcon className="size-3.5" />
          </button>
        </TooltipTrigger>
        <TooltipContent
          side="top"
          sideOffset={8}
          className="max-w-xs rounded-xl"
        >
          {tooltip}
        </TooltipContent>
      </Tooltip>
    </div>
  );
}

function AdminField({
  label,
  tooltip,
  className,
  children,
}: React.PropsWithChildren<{
  label: string;
  tooltip: string;
  className?: string;
}>) {
  return (
    <div className={cn("space-y-2", className)}>
      <AdminLabel label={label} tooltip={tooltip} />
      {children}
    </div>
  );
}

function buildContractCorePayload(
  contract: ContractDraft,
): SaveAllContratoCorePayload {
  return {
    updated_at: contract.updated_at,
    status: contract.status,
    valor_bruto: contract.valor_bruto,
    valor_liquido: contract.valor_liquido,
    valor_mensalidade: contract.valor_mensalidade,
    prazo_meses: Number(contract.prazo_meses ?? 3),
    taxa_antecipacao: contract.taxa_antecipacao,
    margem_disponivel: contract.margem_disponivel,
    valor_total_antecipacao: contract.valor_total_antecipacao,
    doacao_associado: contract.doacao_associado,
    comissao_agente: contract.comissao_agente,
    data_contrato: contract.data_contrato,
    data_aprovacao: contract.data_aprovacao,
    data_primeira_mensalidade: contract.data_primeira_mensalidade,
    mes_averbacao: contract.mes_averbacao,
    auxilio_liberado_em: contract.auxilio_liberado_em,
  };
}

function normalizeCycleStatusForEditor(status: string) {
  return status === "fechado" ? "concluido" : status;
}

function normalizeCycleStatusForApi(status: string) {
  return status === "concluido" ? "fechado" : status;
}

export function buildCyclesPayload(contract: ContractDraft): SaveAllCyclesPayload {
  const outOfCycleRows = [
    ...contract.meses_nao_pagos,
    ...contract.movimentos_financeiros_avulsos,
  ];
  const hasNoCycles = contract.ciclos.length === 0;
  const fallbackCycleClientKey = "auto-fallback-cycle-1";
  const fallbackReferences = outOfCycleRows
    .map((row) => row.referencia_mes)
    .filter((value) => Boolean(value))
    .sort((left, right) => (monthOrder(left) ?? 0) - (monthOrder(right) ?? 0));
  const fallbackStart = fallbackReferences[0] ?? new Date().toISOString().slice(0, 10);
  const fallbackEnd = fallbackReferences[fallbackReferences.length - 1] ?? fallbackStart;
  const fallbackTotal = centsToDecimal(
    outOfCycleRows.reduce(
      (total, row) => total + (decimalToCents(row.valor || "0.00") ?? 0),
      0,
    ),
  );
  const payloadCycles =
    hasNoCycles && outOfCycleRows.length > 0
      ? [
          {
            id: null,
            client_key: fallbackCycleClientKey,
            numero: 1,
            data_inicio: fallbackStart,
            data_fim: fallbackEnd,
            status: "aberto",
            valor_total: fallbackTotal,
          },
        ]
      : contract.ciclos.map((cycle) => ({
          id: cycle.id,
          client_key: cycle.client_key,
          numero: cycle.numero,
          data_inicio: cycle.data_inicio,
          data_fim: cycle.data_fim,
          status: normalizeCycleStatusForApi(cycle.status),
          valor_total: cycle.valor_total,
        }));
  const primaryCycleRef =
    payloadCycles[0]?.id != null
      ? String(payloadCycles[0].id)
      : payloadCycles[0]?.client_key || "";

  return {
    updated_at: contract.updated_at,
    cycles: payloadCycles,
    parcelas: [
      ...contract.ciclos.flatMap((cycle) =>
        cycle.parcelas.map((parcela) => ({
          id: parcela.id,
          cycle_ref:
            cycle.id != null ? String(cycle.id) : cycle.client_key || "",
          numero: parcela.numero,
          referencia_mes: parcela.referencia_mes,
          valor: parcela.valor,
          data_vencimento: parcela.data_vencimento,
          status: parcela.status,
          data_pagamento: parcela.data_pagamento,
          observacao: parcela.observacao,
          layout_bucket: "cycle",
        })),
      ),
      ...contract.meses_nao_pagos.map((parcela) => ({
        id: parcela.id,
        cycle_ref: primaryCycleRef,
        numero: parcela.numero,
        referencia_mes: parcela.referencia_mes,
        valor: parcela.valor,
        data_vencimento: parcela.data_vencimento,
        status: parcela.status,
        data_pagamento: parcela.data_pagamento,
        observacao: parcela.observacao,
        layout_bucket: "unpaid",
      })),
      ...contract.movimentos_financeiros_avulsos.map((parcela) => ({
        id: parcela.id,
        cycle_ref: primaryCycleRef,
        numero: parcela.numero,
        referencia_mes: parcela.referencia_mes,
        valor: parcela.valor,
        data_vencimento: parcela.data_vencimento,
        status: parcela.status,
        data_pagamento: parcela.data_pagamento,
        observacao: parcela.observacao,
        layout_bucket: "movement",
      })),
    ],
  };
}

export function buildRefinanciamentoPayload(
  refinanciamento: ContractDraft["refinanciamento_ativo"],
): SaveAllRefinanciamentoPayload | null {
  if (!refinanciamento) {
    return null;
  }

  return {
    id: refinanciamento.id,
    updated_at: refinanciamento.updated_at,
    valor_refinanciamento: refinanciamento.valor_refinanciamento,
    repasse_agente: refinanciamento.repasse_agente,
    competencia_solicitada: refinanciamento.competencia_solicitada,
    observacao: refinanciamento.observacao,
    analista_note: refinanciamento.analista_note,
    coordenador_note: refinanciamento.coordenador_note,
  };
}

function isPayloadDirty<T>(left: T, right: T) {
  return JSON.stringify(left) !== JSON.stringify(right);
}

function createContractDraft(contract: AdminEditorContrato): ContractDraft {
  return {
    ...contract,
    ciclos: contract.ciclos.map((cycle) => ({
      ...cycle,
      status: normalizeCycleStatusForEditor(cycle.status),
    })),
  };
}

const AdminContractEditor = React.forwardRef<AdminContractEditorHandle, Props>(
  function AdminContractEditor(
    {
      associadoId,
      contract,
      onPayloadRefresh,
      onDirtyChange,
      renewalStageOptions = [],
      selectedRenewalStage,
      onSelectedRenewalStageChange,
      onRequestRenewalStageTransition,
      renewalTransitionDisabled = false,
      renewalTransitionPending = false,
    }: Props,
    ref,
  ) {
    const [draft, setDraft] = React.useState<ContractDraft>(
      createContractDraft(contract),
    );
    const [pendingCycleUploads, setPendingCycleUploads] = React.useState<
      Record<string, File[]>
    >({});
    const queryClient = useQueryClient();
    const onPayloadRefreshEvent = React.useEffectEvent(
      (payload?: AdminAssociadoEditorPayload) => {
        return onPayloadRefresh(payload);
      },
    );
    const onDirtyChangeEvent = React.useEffectEvent(
      (state: ContractEditorDirtyState) => {
        onDirtyChange?.(state);
      },
    );

    React.useEffect(() => {
      setDraft(createContractDraft(contract));
      setPendingCycleUploads({});
    }, [contract]);

    const refresh = async (payload?: AdminAssociadoEditorPayload) => {
      await Promise.all([
        onPayloadRefreshEvent(payload),
        queryClient.invalidateQueries({ queryKey: ["associado", associadoId] }),
        queryClient.invalidateQueries({
          queryKey: ["admin-associado-editor", associadoId],
        }),
        queryClient.invalidateQueries({
          queryKey: ["admin-associado-history", associadoId],
        }),
      ]);
    };

    const initialCorePayload = React.useMemo(
      () => buildContractCorePayload(createContractDraft(contract)),
      [contract],
    );
    const currentCorePayload = React.useMemo(
      () => buildContractCorePayload(draft),
      [draft],
    );
    const initialCyclesPayload = React.useMemo(
      () => buildCyclesPayload(createContractDraft(contract)),
      [contract],
    );
    const currentCyclesPayload = React.useMemo(
      () => buildCyclesPayload(draft),
      [draft],
    );
    const initialRefiPayload = React.useMemo(
      () =>
        buildRefinanciamentoPayload(
          createContractDraft(contract).refinanciamento_ativo,
        ),
      [contract],
    );
    const currentRefiPayload = React.useMemo(
      () => buildRefinanciamentoPayload(draft.refinanciamento_ativo),
      [draft.refinanciamento_ativo],
    );
    const dirtyState = React.useMemo<ContractEditorDirtyState>(
      () => ({
        core: isPayloadDirty(initialCorePayload, currentCorePayload),
        cycles: isPayloadDirty(initialCyclesPayload, currentCyclesPayload),
        refinanciamento: isPayloadDirty(initialRefiPayload, currentRefiPayload),
      }),
      [
        currentCorePayload,
        currentCyclesPayload,
        currentRefiPayload,
        initialCorePayload,
        initialCyclesPayload,
        initialRefiPayload,
      ],
    );

    React.useEffect(() => {
      onDirtyChangeEvent?.(dirtyState);
    }, [dirtyState]);

    React.useImperativeHandle(
      ref,
      () => ({
        getPendingChanges() {
          const dirtySections: Array<
            "contract_core" | "cycle_layout" | "refinanciamento"
          > = [];
          const pending: AdminContractEditorPendingChanges = {
            id: contract.id,
          };

          if (dirtyState.core) {
            dirtySections.push("contract_core");
            pending.core = currentCorePayload;
          }
          if (dirtyState.cycles) {
            dirtySections.push("cycle_layout");
            pending.cycles = currentCyclesPayload;
          }
          if (dirtyState.refinanciamento && currentRefiPayload) {
            dirtySections.push("refinanciamento");
            pending.refinanciamento = currentRefiPayload;
          }
          if (dirtySections.length) {
            pending.dirty_sections = dirtySections;
          }

          return pending.core || pending.cycles || pending.refinanciamento
            ? pending
            : null;
        },
        hasPendingChanges() {
          return (
            dirtyState.core || dirtyState.cycles || dirtyState.refinanciamento
          );
        },
      }),
      [
        contract.id,
        currentCorePayload,
        currentCyclesPayload,
        currentRefiPayload,
        dirtyState,
      ],
    );

    const cycleUploadMutation = useMutation({
      mutationFn: async ({
        cycleId,
        files,
      }: {
        cycleId: number;
        files: File[];
      }) => {
        const formData = new FormData();
        formData.append("ciclo_id", String(cycleId));
        formData.append(
          "motivo",
          "Inclusão administrativa de comprovantes no ciclo.",
        );
        formData.append("tipo", "outro");
        formData.append("papel", "operacional");
        formData.append("origem", "outro");
        formData.append("status_validacao", "pendente");
        files.forEach((file) => formData.append("arquivos", file));
        return apiFetch<AdminAssociadoEditorPayload>(
          "admin-overrides/comprovantes/",
          {
            method: "POST",
            formData,
          },
        );
      },
      onSuccess: async (payload, variables) => {
        toast.success("Comprovantes anexados ao ciclo.");
        setPendingCycleUploads((current) => {
          const next = { ...current };
          delete next[String(variables.cycleId)];
          return next;
        });
        await refresh(payload);
      },
      onError: (error) => {
        toast.error(
          error instanceof Error
            ? error.message
            : "Falha ao anexar comprovantes ao ciclo.",
        );
      },
    });

    const cycleDraftKey = React.useCallback(
      (cycle: Pick<AdminEditorCiclo, "id" | "client_key">) =>
        String(cycle.id ?? cycle.client_key ?? ""),
      [],
    );

    const updateCycle = (index: number, patch: Partial<AdminEditorCiclo>) => {
      setDraft((current) => {
        const ciclos = current.ciclos.map((cycle, cycleIndex) =>
          cycleIndex === index ? { ...cycle, ...patch } : cycle,
        );
        return { ...current, ciclos };
      });
    };

    const updateParcela = (
      cycleIndex: number,
      parcelaIndex: number,
      patch: Partial<AdminEditorParcela>,
    ) => {
      setDraft((current) => {
        const ciclos = current.ciclos.map((cycle, currentCycleIndex) => {
          if (currentCycleIndex !== cycleIndex) {
            return cycle;
          }
          return {
            ...cycle,
            parcelas: cycle.parcelas.map((parcela, currentParcelaIndex) =>
              currentParcelaIndex === parcelaIndex
                ? { ...parcela, ...patch }
                : parcela,
            ),
          };
        });
        return { ...current, ciclos };
      });
    };

    const moveParcela = (
      cycleIndex: number,
      parcelaIndex: number,
      direction: -1 | 1,
    ) => {
      setDraft((current) => {
        const targetCycleIndex = cycleIndex + direction;
        if (targetCycleIndex < 0 || targetCycleIndex >= current.ciclos.length) {
          return current;
        }
        const sourceCycle = current.ciclos[cycleIndex];
        const targetCycle = current.ciclos[targetCycleIndex];
        const parcela = sourceCycle.parcelas[parcelaIndex];
        const nextParcel = {
          ...parcela,
          numero: targetCycle.parcelas.length + 1,
        };
        const ciclos = current.ciclos.map((cycle, index) => {
          if (index === cycleIndex) {
            return {
              ...cycle,
              parcelas: cycle.parcelas
                .filter((_, indexParcela) => indexParcela !== parcelaIndex)
                .map((item, indexParcela) => ({
                  ...item,
                  numero: indexParcela + 1,
                })),
            };
          }
          if (index === targetCycleIndex) {
            return {
              ...cycle,
              parcelas: [...cycle.parcelas, nextParcel],
            };
          }
          return cycle;
        });
        return { ...current, ciclos };
      });
    };

    const addCycle = () => {
      const number = nextCycleNumber(draft);
      setDraft((current) => ({
        ...current,
        ciclos: [
          ...current.ciclos,
          {
            id: null,
            client_key: crypto.randomUUID(),
            numero: number,
            data_inicio: new Date().toISOString().slice(0, 10),
            data_fim: new Date().toISOString().slice(0, 10),
            status: "aberto",
            valor_total: "0.00",
            updated_at: null,
            comprovantes_ciclo: [],
            termo_antecipacao: null,
            parcelas: [],
          },
        ],
      }));
    };

    const removeCycle = (cycleIndex: number) => {
      setDraft((current) => ({
        ...current,
        ciclos: current.ciclos
          .filter((_, index) => index !== cycleIndex)
          .map((cycle, index) => ({ ...cycle, numero: index + 1 })),
      }));
    };

    const addParcela = (cycleIndex: number) => {
      setDraft((current) => ({
        ...current,
        ciclos: current.ciclos.map((cycle, index) =>
          index === cycleIndex
            ? {
                ...cycle,
                parcelas: [
                  ...cycle.parcelas,
                  buildBlankParcel(cycle.parcelas.length + 1),
                ],
              }
            : cycle,
        ),
      }));
    };

    const removeParcela = (cycleIndex: number, parcelaIndex: number) => {
      setDraft((current) => ({
        ...current,
        ciclos: current.ciclos.map((cycle, index) =>
          index === cycleIndex
            ? {
                ...cycle,
                parcelas: cycle.parcelas
                  .filter(
                    (_, currentParcelaIndex) =>
                      currentParcelaIndex !== parcelaIndex,
                  )
                  .map((item, indexParcela) => ({
                    ...item,
                    numero: indexParcela + 1,
                  })),
              }
            : cycle,
        ),
      }));
    };

    const moveExtraParcelaToCycle = (
      bucket: "meses_nao_pagos" | "movimentos_financeiros_avulsos",
      index: number,
    ) => {
      let missingCompatibleCycle = false;
      let duplicateReference = false;

      setDraft((current) => {
        const parcela = current[bucket][index];
        if (!parcela) {
          return current;
        }

        const reference = monthOrder(parcela.referencia_mes);
        const targetCycleIndex = current.ciclos.findIndex((cycle) => {
          const start = monthOrder(cycle.data_inicio);
          const end = monthOrder(cycle.data_fim);
          return (
            reference !== null &&
            start !== null &&
            end !== null &&
            reference >= start &&
            reference <= end
          );
        });

        if (targetCycleIndex < 0) {
          missingCompatibleCycle = true;
          return current;
        }

        const targetCycle = current.ciclos[targetCycleIndex];
        if (
          targetCycle.parcelas.some(
            (item) => item.referencia_mes === parcela.referencia_mes,
          )
        ) {
          duplicateReference = true;
          return current;
        }

        return {
          ...current,
          [bucket]: current[bucket].filter(
            (_, currentIndex) => currentIndex !== index,
          ),
          ciclos: current.ciclos.map((cycle, cycleIndex) =>
            cycleIndex === targetCycleIndex
              ? {
                  ...cycle,
                  parcelas: [
                    ...cycle.parcelas,
                    { ...parcela, layout_bucket: "cycle" },
                  ]
                    .sort((left, right) => {
                      const leftOrder = monthOrder(left.referencia_mes);
                      const rightOrder = monthOrder(right.referencia_mes);
                      if (
                        leftOrder == null ||
                        rightOrder == null ||
                        leftOrder === rightOrder
                      ) {
                        return left.numero - right.numero;
                      }
                      return leftOrder - rightOrder;
                    })
                    .map((item, itemIndex) => ({
                      ...item,
                      numero: itemIndex + 1,
                    })),
                }
              : cycle,
          ),
        };
      });

      if (missingCompatibleCycle) {
        toast.error(
          "Nenhum ciclo compatível com a competência. Ajuste o intervalo do ciclo antes de mover.",
        );
      } else if (duplicateReference) {
        toast.error("Essa competência já está presente no ciclo compatível.");
      }
    };

    const renderCycleComprovantes = (cycle: AdminEditorCiclo) => {
      const key = cycleDraftKey(cycle);
      const pendingFiles = pendingCycleUploads[key] ?? [];
      const existingFiles: ComprovanteCiclo[] = [
        ...(cycle.termo_antecipacao ? [cycle.termo_antecipacao] : []),
        ...cycle.comprovantes_ciclo,
      ];

      return (
        <div className="space-y-3 rounded-2xl border border-dashed border-border/60 bg-card/40 p-3">
          <AdminLabel
            label="Comprovantes do ciclo"
            tooltip="Anexe comprovantes e outros arquivos vinculados diretamente a este ciclo. Os arquivos entram no histórico administrativo e ficam versionáveis."
          />
          {existingFiles.length ? (
            <div className="space-y-2">
              {existingFiles.map((arquivo) => (
                <div
                  key={`${cycle.id ?? cycle.client_key}-${arquivo.id ?? arquivo.arquivo_referencia}`}
                  className="flex items-center justify-between gap-3 rounded-xl border border-border/60 bg-background/60 px-3 py-2 text-sm"
                >
                  <div className="min-w-0">
                    <p className="truncate font-medium capitalize">
                      {arquivo.tipo.replaceAll("_", " ")}
                    </p>
                    <p className="truncate text-xs text-muted-foreground">
                      {arquivo.nome_original || arquivo.arquivo_referencia}
                    </p>
                  </div>
                  <div className="flex items-center gap-2">
                    <StatusBadge
                      status={arquivo.status_validacao || "pendente"}
                      label={arquivo.status_validacao?.replaceAll("_", " ")}
                    />
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      onClick={() => openFile(arquivo.arquivo)}
                    >
                      Abrir
                    </Button>
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <p className="text-sm text-muted-foreground">
              Nenhum comprovante anexado a este ciclo ainda.
            </p>
          )}

          <FileUploadDropzone
            multiple
            accept={{
              "application/pdf": [".pdf"],
              "image/jpeg": [".jpg", ".jpeg"],
              "image/png": [".png"],
            }}
            files={pendingFiles}
            onUploadMany={(files) =>
              setPendingCycleUploads((current) => ({
                ...current,
                [key]: files,
              }))
            }
            disabled={cycle.id == null || cycleUploadMutation.isPending}
            className="rounded-2xl px-4 py-6"
            emptyTitle="Anexar comprovantes do ciclo"
            emptyDescription={
              cycle.id == null
                ? "Salve o layout dos ciclos antes de anexar arquivos neste ciclo."
                : "PDF, JPG ou PNG. Você pode enviar mais de um arquivo."
            }
          />
          <div className="flex justify-end">
            <Button
              type="button"
              variant="outline"
              disabled={
                cycle.id == null ||
                pendingFiles.length === 0 ||
                cycleUploadMutation.isPending
              }
              onClick={() => {
                if (!cycle.id || pendingFiles.length === 0) {
                  return;
                }
                cycleUploadMutation.mutate({
                  cycleId: cycle.id,
                  files: pendingFiles,
                });
              }}
            >
              <PaperclipIcon className="mr-2 size-4" />
              Salvar comprovantes
            </Button>
          </div>
        </div>
      );
    };

    const renderExtraParcelSection = (
      title: string,
      bucket: "meses_nao_pagos" | "movimentos_financeiros_avulsos",
    ) => {
      const items = draft[bucket];
      return (
        <Card className="rounded-[1.5rem] border-border/60 bg-card/60">
          <CardHeader className="flex flex-row items-center justify-between gap-3">
            <CardTitle className="text-base">{title}</CardTitle>
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() =>
                setDraft((current) => ({
                  ...current,
                  [bucket]: [
                    ...current[bucket],
                    buildBlankParcel(current[bucket].length + 1),
                  ],
                }))
              }
            >
              <PlusIcon className="mr-2 size-4" />
              Adicionar
            </Button>
          </CardHeader>
          <CardContent className="grid gap-3 xl:grid-cols-2">
            {items.length ? (
              items.map((item, index) => (
                <div
                  key={`${bucket}-${item.id ?? index}`}
                  className="rounded-2xl border border-border/60 bg-background/60 p-4"
                >
                  <div className="flex items-center justify-between gap-3">
                    <StatusBadge status={item.status} />
                    <div className="flex items-center gap-1">
                      <Button
                        type="button"
                        variant="ghost"
                        size="sm"
                        onClick={() => moveExtraParcelaToCycle(bucket, index)}
                      >
                        <ArrowLeftRightIcon className="mr-2 size-4" />
                        Mover para ciclo compatível
                      </Button>
                      <Button
                        type="button"
                        variant="ghost"
                        size="icon"
                        onClick={() =>
                          setDraft((current) => ({
                            ...current,
                            [bucket]: current[bucket].filter(
                              (_, currentIndex) => currentIndex !== index,
                            ),
                          }))
                        }
                      >
                        <Trash2Icon className="size-4" />
                      </Button>
                    </div>
                  </div>
                  <div className="mt-3 grid gap-3">
                    <AdminField
                      label="Competência"
                      tooltip="Define o mês/ano desta competência fora do ciclo principal."
                    >
                      <CalendarCompetencia
                        value={monthDate(item.referencia_mes)}
                        onChange={(date) =>
                          setDraft((current) => ({
                            ...current,
                            [bucket]: current[bucket].map(
                              (currentItem, currentIndex) =>
                                currentIndex === index
                                  ? {
                                      ...currentItem,
                                      referencia_mes: monthToReference(
                                        `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}`,
                                      ),
                                    }
                                  : currentItem,
                            ),
                          }))
                        }
                      />
                    </AdminField>
                    <AdminField
                      label="Status"
                      tooltip="Controla como a competência será tratada na leitura financeira e operacional."
                    >
                      <Select
                        value={item.status}
                        onValueChange={(value) =>
                          setDraft((current) => ({
                            ...current,
                            [bucket]: current[bucket].map(
                              (currentItem, currentIndex) =>
                                currentIndex === index
                                  ? {
                                      ...currentItem,
                                      status: value,
                                    }
                                  : currentItem,
                            ),
                          }))
                        }
                      >
                        <SelectTrigger>
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          {parcelaStatusOptions.map((status) => (
                            <SelectItem key={status} value={status}>
                              {status.replaceAll("_", " ")}
                            </SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                    </AdminField>
                    <AdminField
                      label="Valor"
                      tooltip="Valor financeiro atribuído a esta competência fora do ciclo."
                    >
                      <InputCurrency
                        value={decimalToCents(item.valor)}
                        onChange={(value) =>
                          setDraft((current) => ({
                            ...current,
                            [bucket]: current[bucket].map(
                              (currentItem, currentIndex) =>
                                currentIndex === index
                                  ? {
                                      ...currentItem,
                                      valor: centsToDecimal(value),
                                    }
                                  : currentItem,
                            ),
                          }))
                        }
                      />
                    </AdminField>
                    <AdminField
                      label="Vencimento"
                      tooltip="Data prevista de vencimento desta competência."
                    >
                      <DatePicker
                        value={parseIsoDate(item.data_vencimento)}
                        onChange={(date) =>
                          setDraft((current) => ({
                            ...current,
                            [bucket]: current[bucket].map(
                              (currentItem, currentIndex) =>
                                currentIndex === index
                                  ? {
                                      ...currentItem,
                                      data_vencimento:
                                        isoDate(date) ||
                                        currentItem.data_vencimento,
                                    }
                                  : currentItem,
                            ),
                          }))
                        }
                      />
                    </AdminField>
                    <AdminField
                      label="Pagamento"
                      tooltip="Data em que a competência foi efetivamente paga ou baixada."
                    >
                      <DatePicker
                        value={parseIsoDate(item.data_pagamento)}
                        onChange={(date) =>
                          setDraft((current) => ({
                            ...current,
                            [bucket]: current[bucket].map(
                              (currentItem, currentIndex) =>
                                currentIndex === index
                                  ? {
                                      ...currentItem,
                                      data_pagamento: isoDate(date),
                                    }
                                  : currentItem,
                            ),
                          }))
                        }
                      />
                    </AdminField>
                    <AdminField
                      label="Observação"
                      tooltip="Descrição livre para justificar ou contextualizar esta competência."
                    >
                      <Textarea
                        rows={3}
                        value={item.observacao}
                        onChange={(event) =>
                          setDraft((current) => ({
                            ...current,
                            [bucket]: current[bucket].map(
                              (currentItem, currentIndex) =>
                                currentIndex === index
                                  ? {
                                      ...currentItem,
                                      observacao: event.target.value,
                                    }
                                  : currentItem,
                            ),
                          }))
                        }
                      />
                    </AdminField>
                  </div>
                </div>
              ))
            ) : (
              <p className="text-sm text-muted-foreground">
                Nenhuma competência nesta seção.
              </p>
            )}
          </CardContent>
        </Card>
      );
    };

    return (
      <TooltipProvider delayDuration={120}>
        <div className="space-y-4">
          <Card className="rounded-[1.5rem] border-primary/20 bg-primary/5">
            <CardHeader className="flex flex-row flex-wrap items-center justify-between gap-3">
              <div>
                <CardTitle className="text-base">Editor avançado</CardTitle>
                <p className="mt-1 text-sm text-muted-foreground">
                  Ajustes do contrato, renovação e layout dos ciclos ficam em
                  rascunho local até o salvamento global do editor.
                </p>
              </div>
              <div className="flex flex-wrap items-center gap-2 text-xs">
                {dirtyState.core ? (
                  <StatusBadge status="pendente" label="Contrato pendente" />
                ) : null}
                {dirtyState.cycles ? (
                  <StatusBadge status="pendente" label="Ciclos pendentes" />
                ) : null}
                {dirtyState.refinanciamento ? (
                  <StatusBadge status="pendente" label="Renovação pendente" />
                ) : null}
                {!dirtyState.core &&
                !dirtyState.cycles &&
                !dirtyState.refinanciamento ? (
                  <div className="inline-flex items-center gap-2 rounded-full border border-emerald-500/30 bg-emerald-500/10 px-3 py-1 text-emerald-200">
                    <CircleAlertIcon className="size-3.5" />
                    Sem alterações pendentes
                  </div>
                ) : null}
              </div>
            </CardHeader>
            <CardContent className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
              <AdminField
                label="Status"
                tooltip="Estado administrativo atual do contrato. Alterar aqui impacta leituras operacionais e relatórios."
              >
                <Select
                  value={draft.status}
                  onValueChange={(value) =>
                    setDraft((current) => ({ ...current, status: value }))
                  }
                >
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {contratoStatusOptions.map((status) => (
                      <SelectItem key={status} value={status}>
                        {status.replaceAll("_", " ")}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </AdminField>
              <AdminField
                label="Valor bruto"
                tooltip="Valor bruto total informado no contrato antes de descontos e composições internas."
              >
                <InputCurrency
                  value={decimalToCents(draft.valor_bruto)}
                  onChange={(value) =>
                    setDraft((current) => ({
                      ...current,
                      valor_bruto: centsToDecimal(value),
                    }))
                  }
                />
              </AdminField>
              <AdminField
                label="Valor líquido"
                tooltip="Valor líquido operacional do contrato usado nas regras financeiras do associado."
              >
                <InputCurrency
                  value={decimalToCents(draft.valor_liquido)}
                  onChange={(value) =>
                    setDraft((current) => ({
                      ...current,
                      valor_liquido: centsToDecimal(value),
                    }))
                  }
                />
              </AdminField>
              <AdminField
                label="Mensalidade"
                tooltip="Valor unitário esperado para cada competência mensal gerada pelo contrato."
              >
                <InputCurrency
                  value={decimalToCents(draft.valor_mensalidade)}
                  onChange={(value) =>
                    setDraft((current) => ({
                      ...current,
                      valor_mensalidade: centsToDecimal(value),
                    }))
                  }
                />
              </AdminField>
              <AdminField
                label="Tamanho do ciclo (parcelas)"
                tooltip="Quantidade de parcelas que compõem cada ciclo de renovação. 3 é o padrão; 4 é a regra alternativa. Mudanças afetam apenas os ciclos futuros."
              >
                <Select
                  value={String(draft.prazo_meses ?? 3)}
                  onValueChange={(value) =>
                    setDraft((current) => ({
                      ...current,
                      prazo_meses: Number(value),
                    }))
                  }
                >
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="3">3 parcelas (padrão)</SelectItem>
                    <SelectItem value="4">4 parcelas</SelectItem>
                  </SelectContent>
                </Select>
              </AdminField>
              <AdminField
                label="Taxa antecipação"
                tooltip="Taxa contratual aplicada na antecipação do auxílio."
              >
                <InputCurrency
                  value={decimalToCents(draft.taxa_antecipacao)}
                  onChange={(value) =>
                    setDraft((current) => ({
                      ...current,
                      taxa_antecipacao: centsToDecimal(value),
                    }))
                  }
                />
              </AdminField>
              <AdminField
                label="Margem disponível"
                tooltip="Parcela disponível para composição financeira após as regras operacionais aplicadas."
              >
                <InputCurrency
                  value={decimalToCents(draft.margem_disponivel)}
                  onChange={(value) =>
                    setDraft((current) => ({
                      ...current,
                      margem_disponivel: centsToDecimal(value),
                    }))
                  }
                />
              </AdminField>
              <AdminField
                label="Valor total antecipação"
                tooltip="Montante total antecipado no contrato considerando o prazo e a mensalidade."
              >
                <InputCurrency
                  value={decimalToCents(draft.valor_total_antecipacao)}
                  onChange={(value) =>
                    setDraft((current) => ({
                      ...current,
                      valor_total_antecipacao: centsToDecimal(value),
                    }))
                  }
                />
              </AdminField>
              <AdminField
                label="Doação associado"
                tooltip="Valor interno classificado como doação ou retenção do associado no contrato."
              >
                <InputCurrency
                  value={decimalToCents(draft.doacao_associado)}
                  onChange={(value) =>
                    setDraft((current) => ({
                      ...current,
                      doacao_associado: centsToDecimal(value),
                    }))
                  }
                />
              </AdminField>
              <AdminField
                label="Comissão agente"
                tooltip="Valor absoluto de comissão do agente vinculado a este contrato."
              >
                <InputCurrency
                  value={decimalToCents(draft.comissao_agente)}
                  onChange={(value) =>
                    setDraft((current) => ({
                      ...current,
                      comissao_agente: centsToDecimal(value),
                    }))
                  }
                />
              </AdminField>
              <AdminField
                label="Data do contrato"
                tooltip="Data-base formal do contrato no sistema."
              >
                <DatePicker
                  value={parseIsoDate(draft.data_contrato)}
                  onChange={(date) =>
                    setDraft((current) => ({
                      ...current,
                      data_contrato: isoDate(date),
                    }))
                  }
                />
              </AdminField>
              <AdminField
                label="Data aprovação"
                tooltip="Data em que o contrato foi considerado aprovado operacionalmente."
              >
                <DatePicker
                  value={parseIsoDate(draft.data_aprovacao)}
                  onChange={(date) =>
                    setDraft((current) => ({
                      ...current,
                      data_aprovacao: isoDate(date),
                    }))
                  }
                />
              </AdminField>
              <AdminField
                label="1ª mensalidade"
                tooltip="Data prevista da primeira mensalidade do contrato."
              >
                <DatePicker
                  value={parseIsoDate(draft.data_primeira_mensalidade)}
                  onChange={(date) =>
                    setDraft((current) => ({
                      ...current,
                      data_primeira_mensalidade: isoDate(date),
                    }))
                  }
                />
              </AdminField>
              <AdminField
                label="Mês averbação"
                tooltip="Mês de averbação usado como referência operacional do contrato."
              >
                <DatePicker
                  value={parseIsoDate(draft.mes_averbacao)}
                  onChange={(date) =>
                    setDraft((current) => ({
                      ...current,
                      mes_averbacao: isoDate(date),
                    }))
                  }
                />
              </AdminField>
              <AdminField
                label="Auxílio liberado em"
                tooltip="Data em que o auxílio foi efetivamente liberado ao associado."
              >
                <DatePicker
                  value={parseIsoDate(draft.auxilio_liberado_em)}
                  onChange={(date) =>
                    setDraft((current) => ({
                      ...current,
                      auxilio_liberado_em: isoDate(date),
                    }))
                  }
                />
              </AdminField>
            </CardContent>
          </Card>

          {draft.refinanciamento_ativo ? (
            <Card className="rounded-[1.5rem] border-border/60 bg-card/60">
              <CardHeader>
                <CardTitle className="text-base">Renovação ativa</CardTitle>
              </CardHeader>
              <CardContent className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
                <AdminField
                  label="Etapa atual"
                  tooltip='O status da renovação é controlado pela transição segura acima e pela tesouraria. O editor avançado não efetiva nem reposiciona a fila diretamente.'
                >
                  <div className="space-y-2 rounded-2xl border border-border/60 bg-background/60 p-3">
                    <StatusBadge
                      status={draft.refinanciamento_ativo.status}
                      label={draft.refinanciamento_ativo.status.replaceAll("_", " ")}
                    />
                    <p className="text-xs text-muted-foreground">
                      Use <strong>Enviar para etapa</strong> para mudar a fila da renovação e a
                      tesouraria para anexar comprovantes e efetivar.
                    </p>
                    {renewalStageOptions.length ? (
                      <div className="grid gap-2 pt-2">
                        <Select
                          value={
                            selectedRenewalStage ??
                            draft.refinanciamento_ativo.status
                          }
                          onValueChange={(value) =>
                            onSelectedRenewalStageChange?.(value)
                          }
                        >
                          <SelectTrigger className="bg-card/60">
                            <SelectValue />
                          </SelectTrigger>
                          <SelectContent>
                            {renewalStageOptions.map((option) => (
                              <SelectItem
                                key={`${draft.id}-${option.value}`}
                                value={option.value}
                              >
                                {option.label}
                              </SelectItem>
                            ))}
                          </SelectContent>
                        </Select>
                        <Button
                          type="button"
                          variant="outline"
                          onClick={onRequestRenewalStageTransition}
                          disabled={
                            renewalTransitionDisabled ||
                            !onRequestRenewalStageTransition
                          }
                        >
                          {renewalTransitionPending
                            ? "Aplicando etapa..."
                            : "Aplicar etapa da renovação"}
                        </Button>
                      </div>
                    ) : null}
                  </div>
                </AdminField>
                <AdminField
                  label="Valor"
                  tooltip="Valor financeiro total atribuído a esta renovação de ciclo."
                >
                  <InputCurrency
                    value={decimalToCents(
                      draft.refinanciamento_ativo.valor_refinanciamento,
                    )}
                    onChange={(value) =>
                      setDraft((current) => ({
                        ...current,
                        refinanciamento_ativo: current.refinanciamento_ativo
                          ? {
                              ...current.refinanciamento_ativo,
                              valor_refinanciamento: centsToDecimal(value),
                            }
                          : null,
                      }))
                    }
                  />
                </AdminField>
                <AdminField
                  label="Repasse agente"
                  tooltip="Valor de repasse financeiro do agente nesta renovação."
                >
                  <InputCurrency
                    value={decimalToCents(
                      draft.refinanciamento_ativo.repasse_agente,
                    )}
                    onChange={(value) =>
                      setDraft((current) => ({
                        ...current,
                        refinanciamento_ativo: current.refinanciamento_ativo
                          ? {
                              ...current.refinanciamento_ativo,
                              repasse_agente: centsToDecimal(value),
                            }
                          : null,
                      }))
                    }
                  />
                </AdminField>
                <AdminField
                  label="Competência"
                  tooltip="Mês de referência solicitado para a renovação."
                >
                  <CalendarCompetencia
                    value={monthDate(
                      draft.refinanciamento_ativo.competencia_solicitada,
                    )}
                    onChange={(date) =>
                      setDraft((current) => ({
                        ...current,
                        refinanciamento_ativo: current.refinanciamento_ativo
                          ? {
                              ...current.refinanciamento_ativo,
                              competencia_solicitada: monthToReference(
                                `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}`,
                              ),
                            }
                          : null,
                      }))
                    }
                  />
                </AdminField>
                <AdminField
                  label="Observação"
                  tooltip="Descrição livre do contexto desta renovação e suas exceções operacionais."
                  className="md:col-span-2"
                >
                  <Textarea
                    rows={3}
                    value={draft.refinanciamento_ativo.observacao}
                    onChange={(event) =>
                      setDraft((current) => ({
                        ...current,
                        refinanciamento_ativo: current.refinanciamento_ativo
                          ? {
                              ...current.refinanciamento_ativo,
                              observacao: event.target.value,
                            }
                          : null,
                      }))
                    }
                  />
                </AdminField>
                <AdminField
                  label="Nota do analista"
                  tooltip="Registro analítico feito pela etapa de análise desta renovação."
                  className="md:col-span-1 xl:col-span-2"
                >
                  <Textarea
                    rows={3}
                    value={draft.refinanciamento_ativo.analista_note}
                    onChange={(event) =>
                      setDraft((current) => ({
                        ...current,
                        refinanciamento_ativo: current.refinanciamento_ativo
                          ? {
                              ...current.refinanciamento_ativo,
                              analista_note: event.target.value,
                            }
                          : null,
                      }))
                    }
                  />
                </AdminField>
                <AdminField
                  label="Nota da coordenação"
                  tooltip="Validação e observações finais registradas pela coordenação."
                  className="md:col-span-1 xl:col-span-2"
                >
                  <Textarea
                    rows={3}
                    value={draft.refinanciamento_ativo.coordenador_note}
                    onChange={(event) =>
                      setDraft((current) => ({
                        ...current,
                        refinanciamento_ativo: current.refinanciamento_ativo
                          ? {
                              ...current.refinanciamento_ativo,
                              coordenador_note: event.target.value,
                            }
                          : null,
                      }))
                    }
                  />
                </AdminField>
              </CardContent>
            </Card>
          ) : null}

          <Card className="rounded-[1.5rem] border-border/60 bg-card/60">
            <CardHeader className="flex flex-row items-center justify-between gap-3">
              <CardTitle className="text-base">Board de ciclos</CardTitle>
              <Button type="button" variant="outline" onClick={addCycle}>
                <PlusIcon className="mr-2 size-4" />
                Adicionar ciclo
              </Button>
            </CardHeader>
            <CardContent className="overflow-x-auto pb-2">
              <div className="flex min-w-max gap-4">
                {draft.ciclos.map((cycle, cycleIndex) => (
                  <div
                    key={cycle.id ?? cycle.client_key ?? cycleIndex}
                    className="w-[22rem] shrink-0 rounded-[1.5rem] border border-border/60 bg-background/60 p-4"
                  >
                    <div className="flex items-center justify-between gap-3">
                      <p className="font-medium">Ciclo {cycle.numero}</p>
                      <Button
                        type="button"
                        variant="ghost"
                        size="icon"
                        onClick={() => removeCycle(cycleIndex)}
                      >
                        <Trash2Icon className="size-4" />
                      </Button>
                    </div>
                    <div className="mt-3 grid gap-3">
                      <AdminField
                        label="Número do ciclo"
                        tooltip="Ordem sequencial do ciclo dentro do contrato."
                      >
                        <Input
                          type="number"
                          min={1}
                          value={cycle.numero}
                          onChange={(event) =>
                            updateCycle(cycleIndex, {
                              numero: Number(event.target.value || 1),
                            })
                          }
                        />
                      </AdminField>
                      <AdminField
                        label="Status do ciclo"
                        tooltip="Situação operacional do ciclo para leitura do contrato e da renovação."
                      >
                        <Select
                          value={cycle.status}
                          onValueChange={(value) =>
                            updateCycle(cycleIndex, { status: value })
                          }
                        >
                          <SelectTrigger>
                            <SelectValue />
                          </SelectTrigger>
                          <SelectContent>
                            {cicloStatusOptions.map((status) => (
                              <SelectItem
                                key={status.value}
                                value={status.value}
                              >
                                {status.label}
                              </SelectItem>
                            ))}
                          </SelectContent>
                        </Select>
                      </AdminField>
                      <AdminField
                        label="Início do ciclo"
                        tooltip="Primeira data-base considerada para este ciclo."
                      >
                        <DatePicker
                          value={parseIsoDate(cycle.data_inicio)}
                          onChange={(date) =>
                            updateCycle(cycleIndex, {
                              data_inicio: isoDate(date) || cycle.data_inicio,
                            })
                          }
                        />
                      </AdminField>
                      <AdminField
                        label="Fim do ciclo"
                        tooltip="Última data-base considerada para este ciclo."
                      >
                        <DatePicker
                          value={parseIsoDate(cycle.data_fim)}
                          onChange={(date) =>
                            updateCycle(cycleIndex, {
                              data_fim: isoDate(date) || cycle.data_fim,
                            })
                          }
                        />
                      </AdminField>
                      <AdminField
                        label="Valor total"
                        tooltip="Soma financeira total atribuída a este ciclo."
                      >
                        <InputCurrency
                          value={decimalToCents(cycle.valor_total)}
                          onChange={(value) =>
                            updateCycle(cycleIndex, {
                              valor_total: centsToDecimal(value),
                            })
                          }
                        />
                      </AdminField>
                    </div>
                    <div className="mt-4 space-y-3">
                      {cycle.parcelas.map((parcela, parcelaIndex) => (
                        <div
                          key={
                            parcela.id ??
                            `${cycleIndex}-${parcelaIndex}-${parcela.referencia_mes}`
                          }
                          className="rounded-2xl border border-border/60 bg-card/60 p-3"
                        >
                          <div className="flex items-center justify-between gap-2">
                            <p className="text-sm font-medium">
                              Parcela {parcela.numero} ·{" "}
                              {formatMonthYear(parcela.referencia_mes)}
                            </p>
                            <div className="flex items-center gap-1">
                              <Button
                                type="button"
                                variant="ghost"
                                size="icon"
                                disabled={cycleIndex === 0}
                                onClick={() =>
                                  moveParcela(cycleIndex, parcelaIndex, -1)
                                }
                              >
                                <ArrowLeftRightIcon className="size-4" />
                              </Button>
                              <Button
                                type="button"
                                variant="ghost"
                                size="icon"
                                disabled={
                                  cycleIndex === draft.ciclos.length - 1
                                }
                                onClick={() =>
                                  moveParcela(cycleIndex, parcelaIndex, 1)
                                }
                              >
                                <ArrowLeftRightIcon className="size-4" />
                              </Button>
                              <Button
                                type="button"
                                variant="ghost"
                                size="icon"
                                onClick={() =>
                                  removeParcela(cycleIndex, parcelaIndex)
                                }
                              >
                                <Trash2Icon className="size-4" />
                              </Button>
                            </div>
                          </div>
                          {parcela.financial_flags.tem_retorno ||
                          parcela.financial_flags.tem_baixa_manual ||
                          parcela.financial_flags.tem_liquidacao ? (
                            <div className="mt-2 flex flex-wrap gap-2 text-[11px] text-amber-200">
                              {parcela.financial_flags.tem_retorno ? (
                                <span>Com retorno</span>
                              ) : null}
                              {parcela.financial_flags.tem_baixa_manual ? (
                                <span>Com baixa manual</span>
                              ) : null}
                              {parcela.financial_flags.tem_liquidacao ? (
                                <span>Com liquidação</span>
                              ) : null}
                            </div>
                          ) : null}
                          <div className="mt-3 grid gap-3">
                            <AdminField
                              label="Competência"
                              tooltip="Mês/ano que esta parcela representa dentro do ciclo."
                            >
                              <CalendarCompetencia
                                value={monthDate(parcela.referencia_mes)}
                                onChange={(date) =>
                                  updateParcela(cycleIndex, parcelaIndex, {
                                    referencia_mes: monthToReference(
                                      `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}`,
                                    ),
                                  })
                                }
                              />
                            </AdminField>
                            <AdminField
                              label="Valor"
                              tooltip="Valor financeiro desta parcela/competência."
                            >
                              <InputCurrency
                                value={decimalToCents(parcela.valor)}
                                onChange={(value) =>
                                  updateParcela(cycleIndex, parcelaIndex, {
                                    valor: centsToDecimal(value),
                                  })
                                }
                              />
                            </AdminField>
                            <AdminField
                              label="Status"
                              tooltip="Situação operacional atual desta parcela."
                            >
                              <Select
                                value={parcela.status}
                                onValueChange={(value) =>
                                  updateParcela(cycleIndex, parcelaIndex, {
                                    status: value,
                                  })
                                }
                              >
                                <SelectTrigger>
                                  <SelectValue />
                                </SelectTrigger>
                                <SelectContent>
                                  {parcelaStatusOptions.map((status) => (
                                    <SelectItem key={status} value={status}>
                                      {status.replaceAll("_", " ")}
                                    </SelectItem>
                                  ))}
                                </SelectContent>
                              </Select>
                            </AdminField>
                            <AdminField
                              label="Vencimento"
                              tooltip="Data prevista de vencimento desta parcela."
                            >
                              <DatePicker
                                value={parseIsoDate(parcela.data_vencimento)}
                                onChange={(date) =>
                                  updateParcela(cycleIndex, parcelaIndex, {
                                    data_vencimento:
                                      isoDate(date) || parcela.data_vencimento,
                                  })
                                }
                              />
                            </AdminField>
                            <AdminField
                              label="Pagamento"
                              tooltip="Data de pagamento ou baixa aplicada a esta parcela."
                            >
                              <DatePicker
                                value={parseIsoDate(parcela.data_pagamento)}
                                onChange={(date) =>
                                  updateParcela(cycleIndex, parcelaIndex, {
                                    data_pagamento: isoDate(date),
                                  })
                                }
                              />
                            </AdminField>
                            <AdminField
                              label="Observação"
                              tooltip="Anotações livres sobre esta parcela e decisões administrativas."
                            >
                              <Textarea
                                rows={3}
                                value={parcela.observacao}
                                onChange={(event) =>
                                  updateParcela(cycleIndex, parcelaIndex, {
                                    observacao: event.target.value,
                                  })
                                }
                              />
                            </AdminField>
                          </div>
                        </div>
                      ))}
                      {renderCycleComprovantes(cycle)}
                      <Button
                        type="button"
                        variant="outline"
                        onClick={() => addParcela(cycleIndex)}
                      >
                        <PlusIcon className="mr-2 size-4" />
                        Adicionar parcela
                      </Button>
                    </div>
                  </div>
                ))}
              </div>
            </CardContent>
          </Card>

          {renderExtraParcelSection(
            "Parcelas não descontadas",
            "meses_nao_pagos",
          )}
          {renderExtraParcelSection(
            "Movimentos financeiros fora do ciclo",
            "movimentos_financeiros_avulsos",
          )}
        </div>
      </TooltipProvider>
    );
  },
);

AdminContractEditor.displayName = "AdminContractEditor";

export default AdminContractEditor;
