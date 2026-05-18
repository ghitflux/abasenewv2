"use client";

import * as React from "react";
import { format } from "date-fns";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  CheckCircle2Icon,
  Clock3Icon,
  HandCoinsIcon,
  SlidersHorizontalIcon,
  Trash2Icon,
  XCircleIcon,
} from "lucide-react";
import { toast } from "sonner";

import type {
  ComprovanteResumo,
  PaginatedResponse,
  RefinanciamentoItem,
  RefinanciamentoResumo,
  SimpleUser,
} from "@/lib/api/types";
import { apiFetch } from "@/lib/api/client";
import { buildBackendFileUrl } from "@/lib/backend-files";
import { formatMonthValue } from "@/lib/date-value";
import { formatCurrency, formatDateTime, formatMonthYear } from "@/lib/formatters";
import {
  describeReportScope,
  exportRouteReport,
  fetchAllPaginatedRows,
  filterRowsByReportScope,
} from "@/lib/reports";
import CalendarCompetencia from "@/components/custom/calendar-competencia";
import DatePicker from "@/components/custom/date-picker";
import StatusBadge from "@/components/custom/status-badge";
import AssociadoDetailsDialog from "@/components/associados/associado-details-dialog";
import CopySnippet from "@/components/shared/copy-snippet";
import DataTable, { type DataTableColumn } from "@/components/shared/data-table";
import ReportExportDialog, {
  type ReportExportFilters,
} from "@/components/shared/report-export-dialog";
import StatsCard from "@/components/shared/stats-card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  NativeSelect,
  NativeSelectOption,
} from "@/components/ui/native-select";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetFooter,
  SheetHeader,
  SheetTitle,
  SheetTrigger,
} from "@/components/ui/sheet";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Skeleton } from "@/components/ui/skeleton";
import { usePermissions } from "@/hooks/use-permissions";

type KpiModalConfig = {
  title: string;
  statuses: string[];
};

const KPI_MODAL_COLUMNS: DataTableColumn<RefinanciamentoItem>[] = [
  {
    id: "associado",
    header: "Associado",
    cell: (row) => (
      <div className="space-y-0.5">
        <p className="text-sm font-medium">{row.associado_nome}</p>
        <p className="font-mono text-xs text-muted-foreground">{row.cpf_cnpj}</p>
      </div>
    ),
  },
  {
    id: "matricula",
    header: "Matrícula",
    cell: (row) => (
      <p className="font-mono text-xs">{row.matricula_display || row.matricula || "—"}</p>
    ),
  },
  {
    id: "contrato",
    header: "Contrato",
    cell: (row) => (
      <p className="font-mono text-xs">{row.contrato_codigo}</p>
    ),
  },
  {
    id: "competencia",
    header: "Competência",
    cell: (row) => formatMonthYear(row.competencia_solicitada),
  },
  {
    id: "agente",
    header: "Agente",
    cell: (row) => (
      <p className="text-xs text-muted-foreground">{row.agente?.full_name || "—"}</p>
    ),
  },
  {
    id: "valor",
    header: "Valor",
    cell: (row) => (
      <div className="space-y-0.5">
        <p className="text-sm font-semibold">{formatCurrency(row.valor_liberado_associado)}</p>
        <p className="text-xs text-emerald-400">Repasse: {formatCurrency(row.repasse_agente)}</p>
      </div>
    ),
  },
  {
    id: "status",
    header: "Status",
    cell: (row) => <StatusBadge status={row.status} />,
  },
  {
    id: "data",
    header: "Data",
    cell: (row) => (
      <p className="text-xs text-muted-foreground">
        {formatDateTime(row.executado_em || row.data_ativacao_ciclo || row.updated_at)}
      </p>
    ),
  },
];

type AgentFilterUser = SimpleUser & {
  email?: string;
  primary_role?: string | null;
};

const PENDING_STATUS = ["aprovado_para_renovacao"];
const EFETIVADO_STATUS = ["efetivado"];
const CANCELADO_STATUS = ["bloqueado", "revertido", "desativado"];
const NUMERO_CICLO_OPTIONS = Array.from({ length: 12 }, (_, index) => String(index + 1));

function toIsoDate(value?: Date) {
  return value ? format(value, "yyyy-MM-dd") : undefined;
}

function normalizeCycleMonths(value: string[]) {
  return Array.from(
    new Set(value.map((item) => item.trim()).filter(Boolean)),
  ).sort((left, right) => left.localeCompare(right));
}

function getCurrentYearRange() {
  const now = new Date();
  return {
    start: new Date(now.getFullYear(), 0, 1),
    end: new Date(now.getFullYear(), 11, 31),
  };
}

function CompactUploadButton({
  id,
  label,
  existingUrl,
  existingReference,
  existingReferenceType,
  existingName,
  draftFile,
  onSelect,
  onClear,
  disabled = false,
}: {
  id: string;
  label: string;
  existingUrl?: string;
  existingReference?: string;
  existingReferenceType?: string;
  existingName?: string;
  draftFile?: File;
  onSelect?: (file: File) => void;
  onClear?: () => void;
  disabled?: boolean;
}) {
  const hasReferenceOnly = Boolean(existingReference && !existingUrl);
  const isLegacyReference = existingReferenceType === "legado_sem_arquivo";
  return (
    <div className="flex min-w-[14rem] items-center gap-2 rounded-xl border border-border/60 bg-background/40 p-2.5">
      <div className="min-w-0 flex-1">
        <p className="text-[11px] font-semibold uppercase tracking-[0.22em] text-muted-foreground">
          {label}
        </p>
        <p className="truncate text-xs text-foreground">
          {draftFile?.name ||
            existingName ||
            (hasReferenceOnly
              ? isLegacyReference
                ? "Arquivo legado vinculado"
                : "Arquivo vinculado por referência"
              : "Sem anexo")}
        </p>
      </div>
      {existingUrl ? (
        <Button asChild size="sm" variant="outline">
          <a href={buildBackendFileUrl(existingUrl)} target="_blank" rel="noreferrer">
            Ver
          </a>
        </Button>
      ) : hasReferenceOnly ? (
        isLegacyReference ? (
          <Badge className="rounded-full bg-amber-500/15 text-amber-200">Legado</Badge>
        ) : (
          <Badge className="rounded-full bg-sky-500/15 text-sky-200">Referência</Badge>
        )
      ) : null}
      {onSelect ? (
        <label className="cursor-pointer">
          <input
            type="file"
            accept=".pdf,.png,.jpg,.jpeg"
            className="hidden"
            disabled={disabled}
            onChange={(event) => {
              const file = event.target.files?.[0];
              if (file) {
                onSelect(file);
              }
              event.currentTarget.value = "";
            }}
            id={id}
          />
          <Button asChild size="sm" variant={draftFile || existingName ? "outline" : "secondary"}>
            <span>{draftFile || existingName || existingReference ? "Trocar" : "Anexar"}</span>
          </Button>
        </label>
      ) : null}
      {draftFile && onClear ? (
        <Button size="icon-sm" variant="ghost" onClick={onClear}>
          <Trash2Icon className="size-4" />
        </Button>
      ) : null}
    </div>
  );
}

function findComprovanteByTipo(
  comprovantes: ComprovanteResumo[],
  tipo: string,
): ComprovanteResumo | undefined {
  return comprovantes.find((item) => item.tipo === tipo);
}

function resolveRenewalAttachments(row: RefinanciamentoItem) {
  return {
    termoAgente: findComprovanteByTipo(row.comprovantes, "termo_antecipacao"),
    comprovanteAssociado: findComprovanteByTipo(
      row.comprovantes,
      "comprovante_pagamento_associado",
    ),
    comprovanteAgente: findComprovanteByTipo(
      row.comprovantes,
      "comprovante_pagamento_agente",
    ),
  };
}

function hasRenewalPaymentProofs(row: RefinanciamentoItem) {
  const { comprovanteAssociado, comprovanteAgente } =
    resolveRenewalAttachments(row);
  return Boolean(comprovanteAssociado && comprovanteAgente);
}

export default function TesourariaRefinanciamentosPage() {
  const currentYearRange = React.useMemo(() => getCurrentYearRange(), []);
  const { hasAnyRole } = usePermissions();
  const canMutate = hasAnyRole(["ADMIN", "TESOUREIRO"]);
  const canManageTermo = hasAnyRole(["ADMIN", "COORDENADOR", "TESOUREIRO"]);
  const canRemoverFila = hasAnyRole(["ADMIN", "COORDENADOR"]);
  const canRetornarPendente = hasAnyRole(["ADMIN", "COORDENADOR", "TESOUREIRO"]);
  const canLimparLinha = hasAnyRole(["ADMIN", "COORDENADOR"]);
  const queryClient = useQueryClient();
  const [search, setSearch] = React.useState("");
  const [isExporting, setIsExporting] = React.useState(false);
  const [excluirTarget, setExcluirTarget] = React.useState<RefinanciamentoItem | null>(null);
  const [retornarTarget, setRetornarTarget] = React.useState<RefinanciamentoItem | null>(null);
  const [limparLinhaTarget, setLimparLinhaTarget] = React.useState<RefinanciamentoItem | null>(null);
  const [efetivarTarget, setEfetivarTarget] = React.useState<RefinanciamentoItem | null>(null);
  const [efetivarCicloParcelas, setEfetivarCicloParcelas] = React.useState<3 | 4>(3);

  const agentesQuery = useQuery({
    queryKey: ["refinanciamentos-agentes"],
    queryFn: () => apiFetch<AgentFilterUser[]>("tesouraria/contratos/agentes"),
    staleTime: 5 * 60 * 1000,
  });

  const agentOptions = React.useMemo(
    () =>
      (agentesQuery.data ?? []).map((item) => ({
        value: String(item.id),
        label: [
          item.full_name || item.email || `Usuário ${item.id}`,
          item.primary_role || null,
        ]
          .filter(Boolean)
          .join(" · "),
      })),
    [agentesQuery.data],
  );
  const [filtersOpen, setFiltersOpen] = React.useState(false);
  const [rangeScope, setRangeScope] = React.useState<"total" | "year" | "custom">("total");
  const [dataInicio, setDataInicio] = React.useState<Date | undefined>(undefined);
  const [dataFim, setDataFim] = React.useState<Date | undefined>(undefined);
  const [cycleMonths, setCycleMonths] = React.useState<string[]>([]);
  const [numeroCiclos, setNumeroCiclos] = React.useState("");
  const [draftRangeScope, setDraftRangeScope] = React.useState<"total" | "year" | "custom">("total");
  const [draftDataInicio, setDraftDataInicio] = React.useState<Date | undefined>(undefined);
  const [draftDataFim, setDraftDataFim] = React.useState<Date | undefined>(undefined);
  const [draftCycleMonths, setDraftCycleMonths] = React.useState<string[]>([]);
  const [draftCycleMonthPicker, setDraftCycleMonthPicker] = React.useState<Date>();
  const [draftNumeroCiclos, setDraftNumeroCiclos] = React.useState("");
  const [pagePending, setPagePending] = React.useState(1);
  const [pageEfetivadas, setPageEfetivadas] = React.useState(1);
  const [pageCanceladas, setPageCanceladas] = React.useState(1);
  const [detailAssociadoId, setDetailAssociadoId] = React.useState<number | null>(null);
  const cycleKey = React.useMemo(() => cycleMonths.join(","), [cycleMonths]);

  React.useEffect(() => {
    setPagePending(1);
    setPageEfetivadas(1);
    setPageCanceladas(1);
  }, [search, dataInicio, dataFim, cycleKey, numeroCiclos]);

  const pendingQuery = useQuery({
    queryKey: [
      "tesouraria-refinanciamentos",
      "pendentes",
      pagePending,
      search,
      dataInicio?.toISOString(),
      dataFim?.toISOString(),
      cycleKey,
      numeroCiclos,
    ],
    queryFn: () =>
      apiFetch<PaginatedResponse<RefinanciamentoItem>>("tesouraria/refinanciamentos", {
        query: {
          page: pagePending,
          page_size: 15,
          search: search || undefined,
          data_inicio: toIsoDate(dataInicio),
          data_fim: toIsoDate(dataFim),
          cycle_key: cycleKey || undefined,
          numero_ciclos: numeroCiclos || undefined,
          status: PENDING_STATUS,
        },
      }),
  });

  const efetivadasQuery = useQuery({
    queryKey: [
      "tesouraria-refinanciamentos",
      "efetivadas",
      pageEfetivadas,
      search,
      dataInicio?.toISOString(),
      dataFim?.toISOString(),
      cycleKey,
      numeroCiclos,
    ],
    queryFn: () =>
      apiFetch<PaginatedResponse<RefinanciamentoItem>>("tesouraria/refinanciamentos", {
        query: {
          page: pageEfetivadas,
          page_size: 15,
          search: search || undefined,
          data_inicio: toIsoDate(dataInicio),
          data_fim: toIsoDate(dataFim),
          cycle_key: cycleKey || undefined,
          numero_ciclos: numeroCiclos || undefined,
          status: EFETIVADO_STATUS,
        },
      }),
  });

  const canceladasQuery = useQuery({
    queryKey: [
      "tesouraria-refinanciamentos",
      "canceladas",
      pageCanceladas,
      search,
      dataInicio?.toISOString(),
      dataFim?.toISOString(),
      cycleKey,
      numeroCiclos,
    ],
    queryFn: () =>
      apiFetch<PaginatedResponse<RefinanciamentoItem>>("tesouraria/refinanciamentos", {
        query: {
          page: pageCanceladas,
          page_size: 15,
          search: search || undefined,
          data_inicio: toIsoDate(dataInicio),
          data_fim: toIsoDate(dataFim),
          cycle_key: cycleKey || undefined,
          numero_ciclos: numeroCiclos || undefined,
          status: CANCELADO_STATUS,
        },
      }),
  });

  const resumoQuery = useQuery({
    queryKey: [
      "tesouraria-refinanciamentos-resumo",
      search,
      dataInicio?.toISOString(),
      dataFim?.toISOString(),
      cycleKey,
      numeroCiclos,
    ],
    queryFn: () =>
      apiFetch<RefinanciamentoResumo>("tesouraria/refinanciamentos/resumo", {
        query: {
          search: search || undefined,
          data_inicio: toIsoDate(dataInicio),
          data_fim: toIsoDate(dataFim),
          cycle_key: cycleKey || undefined,
          numero_ciclos: numeroCiclos || undefined,
        },
      }),
  });

  const substituirComprovanteMutation = useMutation({
    mutationFn: async ({
      refinanciamentoId,
      papel,
      arquivo,
    }: {
      refinanciamentoId: number;
      papel: "associado" | "agente";
      arquivo: File;
    }) => {
      const formData = new FormData();
      formData.set("papel", papel);
      formData.set("arquivo", arquivo);
      return apiFetch<RefinanciamentoItem>(
        `tesouraria/refinanciamentos/${refinanciamentoId}/substituir-comprovante`,
        {
          method: "POST",
          formData,
        },
      );
    },
    onSuccess: (_, variables) => {
      toast.success(
        variables.papel === "associado"
          ? "Comprovante do associado atualizado."
          : "Comprovante do agente atualizado.",
      );
      void queryClient.invalidateQueries({ queryKey: ["tesouraria-refinanciamentos"] });
      void queryClient.invalidateQueries({ queryKey: ["tesouraria-refinanciamentos-resumo"] });
      void queryClient.invalidateQueries({ queryKey: ["agente-refinanciados"] });
    },
    onError: (error) => {
      toast.error(
        error instanceof Error ? error.message : "Falha ao atualizar comprovante.",
      );
    },
  });

  const substituirTermoAgenteMutation = useMutation({
    mutationFn: async ({
      refinanciamentoId,
      arquivo,
    }: {
      refinanciamentoId: number;
      arquivo: File;
    }) => {
      const formData = new FormData();
      formData.set("arquivo", arquivo);
      return apiFetch<RefinanciamentoItem>(
        `tesouraria/refinanciamentos/${refinanciamentoId}/substituir-termo-agente`,
        {
          method: "POST",
          formData,
        },
      );
    },
    onSuccess: () => {
      toast.success("Termo do agente atualizado.");
      void queryClient.invalidateQueries({ queryKey: ["tesouraria-refinanciamentos"] });
      void queryClient.invalidateQueries({ queryKey: ["tesouraria-refinanciamentos-resumo"] });
      void queryClient.invalidateQueries({ queryKey: ["agente-refinanciados"] });
    },
    onError: (error) => {
      toast.error(
        error instanceof Error ? error.message : "Falha ao atualizar termo do agente.",
      );
    },
  });

  const efetivarMutation = useMutation({
    mutationFn: async ({
      refinanciamentoId,
      proximoCicloParcelas,
    }: {
      refinanciamentoId: number;
      proximoCicloParcelas: 3 | 4;
    }) =>
      apiFetch<RefinanciamentoItem>(
        `tesouraria/refinanciamentos/${refinanciamentoId}/efetivar`,
        {
          method: "POST",
          body: { proximo_ciclo_parcelas: proximoCicloParcelas },
        },
      ),
    onSuccess: () => {
      toast.success("Renovação efetivada com sucesso.");
      setEfetivarTarget(null);
      void queryClient.invalidateQueries({
        queryKey: ["tesouraria-refinanciamentos"],
      });
      void queryClient.invalidateQueries({
        queryKey: ["tesouraria-refinanciamentos-resumo"],
      });
      void queryClient.invalidateQueries({ queryKey: ["agente-refinanciados"] });
    },
    onError: (error) => {
      toast.error(
        error instanceof Error
          ? error.message
          : "Falha ao efetivar renovação.",
      );
    },
  });

  const excluirMutation = useMutation({
    mutationFn: async (refinanciamentoId: number) =>
      apiFetch(`tesouraria/refinanciamentos/${refinanciamentoId}/excluir`, {
        method: "POST",
        body: {},
      }),
    onSuccess: () => {
      toast.success("Renovação removida da fila.");
      setExcluirTarget(null);
      void queryClient.invalidateQueries({ queryKey: ["tesouraria-refinanciamentos"] });
      void queryClient.invalidateQueries({ queryKey: ["tesouraria-refinanciamentos-resumo"] });
    },
    onError: (error) => {
      toast.error(error instanceof Error ? error.message : "Falha ao remover da fila.");
    },
  });

  const retornarPendenteMutation = useMutation({
    mutationFn: async (refinanciamentoId: number) =>
      apiFetch<RefinanciamentoItem>(
        `tesouraria/refinanciamentos/${refinanciamentoId}/retornar-pendente`,
        {
          method: "POST",
          body: {},
        },
      ),
    onSuccess: () => {
      toast.success("Renovação devolvida para pendente de pagamento.");
      setRetornarTarget(null);
      void queryClient.invalidateQueries({ queryKey: ["tesouraria-refinanciamentos"] });
      void queryClient.invalidateQueries({ queryKey: ["tesouraria-refinanciamentos-resumo"] });
    },
    onError: (error) => {
      toast.error(
        error instanceof Error
          ? error.message
          : "Falha ao devolver renovação para pendente.",
      );
    },
  });

  const limparLinhaMutation = useMutation({
    mutationFn: async (refinanciamentoId: number) =>
      apiFetch(`tesouraria/refinanciamentos/${refinanciamentoId}/limpar-linha`, {
        method: "POST",
        body: {},
      }),
    onSuccess: () => {
      toast.success("Linha operacional removida.");
      setLimparLinhaTarget(null);
      void queryClient.invalidateQueries({ queryKey: ["tesouraria-refinanciamentos"] });
      void queryClient.invalidateQueries({ queryKey: ["tesouraria-refinanciamentos-resumo"] });
    },
    onError: (error) => {
      toast.error(
        error instanceof Error ? error.message : "Falha ao limpar linha operacional.",
      );
    },
  });

  const [kpiModal, setKpiModal] = React.useState<KpiModalConfig | null>(null);
  const [kpiModalPage, setKpiModalPage] = React.useState(1);

  const kpiModalQuery = useQuery({
    queryKey: [
      "tesouraria-refinanciamentos-kpi-modal",
      kpiModal?.statuses,
      kpiModalPage,
      search,
      dataInicio?.toISOString(),
      dataFim?.toISOString(),
      cycleKey,
      numeroCiclos,
    ],
    queryFn: () =>
      apiFetch<PaginatedResponse<RefinanciamentoItem>>("tesouraria/refinanciamentos", {
        query: {
          page: kpiModalPage,
          page_size: 20,
          search: search || undefined,
          data_inicio: toIsoDate(dataInicio),
          data_fim: toIsoDate(dataFim),
          cycle_key: cycleKey || undefined,
          numero_ciclos: numeroCiclos || undefined,
          status: kpiModal?.statuses,
        },
      }),
    enabled: kpiModal !== null,
  });

  React.useEffect(() => {
    setKpiModalPage(1);
  }, [kpiModal]);

  const resumo = resumoQuery.data;
  const canceladasTotal =
    (resumo?.bloqueados ?? 0) + (resumo?.revertidos ?? 0) + (resumo?.desativados ?? 0);
  const activeFiltersCount =
    Number(rangeScope !== "total") +
    (rangeScope === "custom"
      ? Number(Boolean(dataInicio)) + Number(Boolean(dataFim))
      : 0) +
    Number(cycleMonths.length > 0) +
    Number(Boolean(numeroCiclos.trim()));

  const pendingColumns = React.useMemo<DataTableColumn<RefinanciamentoItem>[]>(
    () => [
      {
        id: "anexos",
        header: "Anexos",
        cellClassName: "min-w-[28rem]",
        cell: (row) => {
          const { termoAgente, comprovanteAssociado, comprovanteAgente } =
            resolveRenewalAttachments(row);
          const hasAllProofs = hasRenewalPaymentProofs(row);
          const isProcessing =
            (substituirTermoAgenteMutation.isPending &&
              substituirTermoAgenteMutation.variables?.refinanciamentoId === row.id) ||
            (substituirComprovanteMutation.isPending &&
              substituirComprovanteMutation.variables?.refinanciamentoId ===
                row.id) ||
            (efetivarMutation.isPending &&
              efetivarMutation.variables?.refinanciamentoId === row.id);

          return (
            <div className="space-y-3">
              <div className="flex flex-wrap gap-2">
                <CompactUploadButton
                  id={`renovacao-${row.id}-termo-agente`}
                  label="Termo do agente"
                  existingUrl={
                    termoAgente?.arquivo_disponivel_localmente ? termoAgente.arquivo : undefined
                  }
                  existingReference={termoAgente?.arquivo_referencia}
                  existingReferenceType={termoAgente?.tipo_referencia}
                  existingName={termoAgente?.nome_original}
                  disabled={!canManageTermo || isProcessing}
                  onSelect={(file) =>
                    substituirTermoAgenteMutation.mutate({
                      refinanciamentoId: row.id,
                      arquivo: file,
                    })
                  }
                  onClear={() => undefined}
                />
                <CompactUploadButton
                  id={`renovacao-${row.id}-associado`}
                  label="Comp. associado"
                  existingUrl={
                    comprovanteAssociado?.arquivo_disponivel_localmente
                      ? comprovanteAssociado.arquivo
                      : undefined
                  }
                  existingReference={comprovanteAssociado?.arquivo_referencia}
                  existingReferenceType={comprovanteAssociado?.tipo_referencia}
                  existingName={comprovanteAssociado?.nome_original}
                  disabled={!canMutate || isProcessing}
                  onSelect={(file) =>
                    substituirComprovanteMutation.mutate({
                      refinanciamentoId: row.id,
                      papel: "associado",
                      arquivo: file,
                    })
                  }
                  onClear={() => undefined}
                />
                <CompactUploadButton
                  id={`renovacao-${row.id}-agente`}
                  label="Comp. agente"
                  existingUrl={
                    comprovanteAgente?.arquivo_disponivel_localmente
                      ? comprovanteAgente.arquivo
                      : undefined
                  }
                  existingReference={comprovanteAgente?.arquivo_referencia}
                  existingReferenceType={comprovanteAgente?.tipo_referencia}
                  existingName={comprovanteAgente?.nome_original}
                  disabled={!canMutate || isProcessing}
                  onSelect={(file) =>
                    substituirComprovanteMutation.mutate({
                      refinanciamentoId: row.id,
                      papel: "agente",
                      arquivo: file,
                    })
                  }
                  onClear={() => undefined}
                />
              </div>
              <p className="text-xs text-muted-foreground">
                {hasAllProofs
                  ? "Os dois comprovantes já estão anexados. Clique em Efetivar renovação para concluir."
                  : "A efetivação exige comprovante do associado e do agente. O upload isolado não conclui a renovação."}
              </p>
            </div>
          );
        },
      },
      {
        id: "acao",
        header: "Ação",
        cell: (row) => (
          <div className="flex min-w-44 flex-col gap-2">
            <Button
              size="sm"
              variant="outline"
              className="w-fit border-emerald-500/40 text-emerald-200"
              onClick={() => {
                const total = row.mensalidades_total;
                setEfetivarCicloParcelas(total === 4 ? 4 : 3);
                setEfetivarTarget(row);
              }}
              disabled={!canMutate || !hasRenewalPaymentProofs(row)}
            >
              <CheckCircle2Icon className="size-4" />
              Efetivar renovação
            </Button>
            {canRemoverFila && (
              <Button
                size="sm"
                variant="outline"
                className="w-fit border-red-500/40 text-red-400"
                onClick={() => setExcluirTarget(row)}
                disabled={excluirMutation.isPending}
              >
                <Trash2Icon className="size-4" />
                Remover da fila
              </Button>
            )}
            <p className="text-xs text-muted-foreground">
              A renovação só é concluída depois dos dois anexos e da ação
              explícita de efetivar.
            </p>
          </div>
        ),
      },
      {
        id: "associado",
        header: "Associado",
        cell: (row) => (
          <div className="space-y-2">
            <CopySnippet
              label="Associado"
              value={row.associado_nome}
              inline
              className="max-w-[16rem]"
            />
            <Button
              size="sm"
              variant="outline"
              onClick={() => setDetailAssociadoId(row.associado_id)}
            >
              Ver detalhes do associado
            </Button>
          </div>
        ),
      },
      {
        id: "contrato",
        header: "Contrato",
        cell: (row) => (
          <div className="space-y-1">
            <CopySnippet label="Contrato" value={row.contrato_codigo} mono inline />
            <p className="text-xs text-muted-foreground">
              Parcelas pagas no ciclo: {row.mensalidades_pagas}/{row.mensalidades_total}
            </p>
          </div>
        ),
      },
      {
        id: "matricula_cpf",
        header: "Matrícula / CPF",
        cell: (row) => (
          <div className="space-y-1">
            <CopySnippet
              label="Matrícula"
              value={row.matricula_display || row.matricula}
              mono
              inline
            />
            <CopySnippet label="CPF" value={row.cpf_cnpj} mono inline />
          </div>
        ),
      },
      {
        id: "agente",
        header: "Agente",
        cell: (row) => (
          <p className="text-sm text-muted-foreground">{row.agente?.full_name || "—"}</p>
        ),
      },
      {
        id: "valores",
        header: "Valor / Repasse",
        cell: (row) => (
          <div className="space-y-1">
            <p className="font-semibold">{formatCurrency(row.valor_liberado_associado)}</p>
            <p className="text-xs text-emerald-400">
              Repasse do agente: {formatCurrency(row.repasse_agente)}
            </p>
          </div>
        ),
      },
      {
        id: "competencia",
        header: "Competência solicitada",
        cell: (row) => formatMonthYear(row.competencia_solicitada),
      },
      {
        id: "envio_tesouraria",
        header: "Envio para tesouraria",
        cell: (row) => formatDateTime(row.updated_at),
      },
      {
        id: "data_anexo_associado",
        header: "Anexo associado",
        cell: (row) => formatDateTime(row.data_anexo_associado),
      },
      {
        id: "data_anexo_agente",
        header: "Anexo agente",
        cell: (row) => formatDateTime(row.data_anexo_agente),
      },
      {
        id: "data_pagamento_associado",
        header: "Pagamento associado",
        cell: (row) => formatDateTime(row.data_pagamento_associado),
      },
      {
        id: "data_pagamento_agente",
        header: "Pagamento agente",
        cell: (row) => formatDateTime(row.data_pagamento_agente),
      },
      {
        id: "status",
        header: "Status",
        cell: (row) => (
          <div className="space-y-2">
            <StatusBadge status={row.status} />
            <p className="text-xs text-muted-foreground">
              {row.coordenador_note?.trim() || row.analista_note?.trim() || row.motivo_apto_renovacao}
            </p>
          </div>
        ),
      },
    ],
    [
      canManageTermo,
      canMutate,
      canRemoverFila,
      efetivarMutation,
      excluirMutation,
      substituirComprovanteMutation,
      substituirTermoAgenteMutation,
    ],
  );

  const historyColumns = React.useMemo<DataTableColumn<RefinanciamentoItem>[]>(
    () => [
      {
        id: "anexos",
        header: "Anexos",
        cellClassName: "min-w-[28rem]",
        cell: (row) => {
          const { termoAgente, comprovanteAssociado, comprovanteAgente } =
            resolveRenewalAttachments(row);
          const isProcessing =
            (substituirTermoAgenteMutation.isPending &&
              substituirTermoAgenteMutation.variables?.refinanciamentoId === row.id) ||
            (substituirComprovanteMutation.isPending &&
              substituirComprovanteMutation.variables?.refinanciamentoId === row.id);

          return (
            <div className="flex flex-wrap gap-2">
              <CompactUploadButton
                id={`readonly-${row.id}-termo-agente`}
                label="Termo do agente"
                existingUrl={
                  termoAgente?.arquivo_disponivel_localmente ? termoAgente.arquivo : undefined
                }
                existingReference={termoAgente?.arquivo_referencia}
                existingReferenceType={termoAgente?.tipo_referencia}
                existingName={termoAgente?.nome_original}
                disabled={!canManageTermo || isProcessing}
                onSelect={(file) =>
                  substituirTermoAgenteMutation.mutate({
                    refinanciamentoId: row.id,
                    arquivo: file,
                  })
                }
                onClear={() => undefined}
              />
              <CompactUploadButton
                id={`readonly-${row.id}-associado`}
                label="Comp. associado"
                existingUrl={
                  comprovanteAssociado?.arquivo_disponivel_localmente
                    ? comprovanteAssociado.arquivo
                    : undefined
                }
                existingReference={comprovanteAssociado?.arquivo_referencia}
                existingReferenceType={comprovanteAssociado?.tipo_referencia}
                existingName={comprovanteAssociado?.nome_original}
                disabled={!canMutate || isProcessing}
                onSelect={(file) =>
                  substituirComprovanteMutation.mutate({
                    refinanciamentoId: row.id,
                    papel: "associado",
                    arquivo: file,
                  })
                }
                onClear={() => undefined}
              />
              <CompactUploadButton
                id={`readonly-${row.id}-agente`}
                label="Comp. agente"
                existingUrl={
                  comprovanteAgente?.arquivo_disponivel_localmente
                    ? comprovanteAgente.arquivo
                    : undefined
                }
                existingReference={comprovanteAgente?.arquivo_referencia}
                existingReferenceType={comprovanteAgente?.tipo_referencia}
                existingName={comprovanteAgente?.nome_original}
                disabled={!canMutate || isProcessing}
                onSelect={(file) =>
                  substituirComprovanteMutation.mutate({
                    refinanciamentoId: row.id,
                    papel: "agente",
                    arquivo: file,
                  })
                }
                onClear={() => undefined}
              />
            </div>
          );
        },
      },
      {
        id: "acao",
        header: "Ação",
        cell: (row) => (
          <div className="flex min-w-52 flex-col gap-2">
            {canRetornarPendente ? (
              <Button
                size="sm"
                variant="outline"
                className="w-fit border-sky-500/40 text-sky-200"
                onClick={() => setRetornarTarget(row)}
                disabled={retornarPendenteMutation.isPending}
              >
                Voltar para pendente
              </Button>
            ) : null}
            {canLimparLinha ? (
              <Button
                size="sm"
                variant="outline"
                className="w-fit border-red-500/40 text-red-400"
                onClick={() => setLimparLinhaTarget(row)}
                disabled={limparLinhaMutation.isPending}
              >
                Limpar linha
              </Button>
            ) : null}
            <p className="text-xs text-muted-foreground">
              Use retorno para recolocar na fila ou limpe linhas operacionais incorretas.
            </p>
          </div>
        ),
      },
      {
        id: "associado",
        header: "Associado",
        cell: (row) => (
          <div className="space-y-1">
            <CopySnippet label="Associado" value={row.associado_nome} inline className="max-w-[16rem]" />
            <CopySnippet label="CPF" value={row.cpf_cnpj} mono inline />
            <Button
              size="sm"
              variant="outline"
              onClick={() => setDetailAssociadoId(row.associado_id)}
            >
              Ver detalhes do associado
            </Button>
          </div>
        ),
      },
      {
        id: "contrato",
        header: "Contrato",
        cell: (row) => (
          <div className="space-y-1">
            <CopySnippet label="Contrato" value={row.contrato_codigo} mono inline />
            <p className="text-xs text-muted-foreground">
              Competência solicitada {formatMonthYear(row.competencia_solicitada)}
            </p>
          </div>
        ),
      },
      {
        id: "agente",
        header: "Agente",
        cell: (row) => (
          <p className="text-sm text-muted-foreground">{row.agente?.full_name || "—"}</p>
        ),
      },
      {
        id: "valor_pago",
        header: "Valor / Repasse",
        cell: (row) => (
          <div className="space-y-1">
            <p className="font-semibold">{formatCurrency(row.valor_liberado_associado)}</p>
            <p className="text-xs text-emerald-400">
              Repasse do agente: {formatCurrency(row.repasse_agente)}
            </p>
          </div>
        ),
      },
      {
        id: "data_operacional",
        header: "Data operacional",
        cell: (row) =>
          formatDateTime(row.executado_em || row.data_ativacao_ciclo || row.updated_at),
      },
      {
        id: "data_anexo_associado",
        header: "Anexo associado",
        cell: (row) => formatDateTime(row.data_anexo_associado),
      },
      {
        id: "data_anexo_agente",
        header: "Anexo agente",
        cell: (row) => formatDateTime(row.data_anexo_agente),
      },
      {
        id: "data_pagamento_associado",
        header: "Pagamento associado",
        cell: (row) => formatDateTime(row.data_pagamento_associado),
      },
      {
        id: "data_pagamento_agente",
        header: "Pagamento agente",
        cell: (row) => formatDateTime(row.data_pagamento_agente),
      },
      {
        id: "status",
        header: "Status",
        cell: (row) => (
          <div className="space-y-2">
            <StatusBadge status={row.status} />
            {row.data_ativacao_ciclo ? (
              <p className="text-xs text-muted-foreground">
                Ativado em {formatDateTime(row.data_ativacao_ciclo)}
              </p>
            ) : null}
          </div>
        ),
      },
    ],
    [
      canLimparLinha,
      canManageTermo,
      canMutate,
      canRetornarPendente,
      limparLinhaMutation,
      retornarPendenteMutation,
      substituirComprovanteMutation,
      substituirTermoAgenteMutation,
    ],
  );

  const handleExport = React.useCallback(
    async (exportFilters: ReportExportFilters, exportFormat: "pdf" | "xlsx") => {
      const {
        scope,
        referenceDate,
        agente: exportAgente,
        status: exportStatus,
        columns: selectedColumns,
      } = exportFilters;
      setIsExporting(true);
      try {
        const sourceQuery = {
          search: search || undefined,
          data_inicio: toIsoDate(dataInicio),
          data_fim: toIsoDate(dataFim),
          agente: exportAgente || undefined,
          cycle_key: cycleKey || undefined,
          numero_ciclos: numeroCiclos || undefined,
        };
        const fetchedRows = await fetchAllPaginatedRows<RefinanciamentoItem>({
          sourcePath: "tesouraria/refinanciamentos",
          sourceQuery,
        });
        const scopedRows = filterRowsByReportScope({
          rows: fetchedRows,
          scope,
          referenceDate,
          getCandidates: (row) => [
            row.data_solicitacao_renovacao,
            row.data_solicitacao,
            row.executado_em,
            row.data_ativacao_ciclo,
            row.updated_at,
          ],
        }).filter((row) => !exportStatus || row.status === exportStatus);

        const rows = scopedRows.map((row) => ({
          associado_nome: row.associado_nome,
          cpf_cnpj: row.cpf_cnpj,
          contrato_codigo: row.contrato_codigo,
          data_solicitacao: formatDateTime(
            row.data_solicitacao_renovacao || row.data_solicitacao,
          ),
          data_anexo_associado: formatDateTime(row.data_anexo_associado),
          data_anexo_agente: formatDateTime(row.data_anexo_agente),
          data_pagamento_associado: formatDateTime(row.data_pagamento_associado),
          data_pagamento_agente: formatDateTime(row.data_pagamento_agente),
          status: row.status,
          valor_refinanciamento: formatCurrency(row.valor_liberado_associado),
          repasse_agente: row.repasse_agente,
          pagamento_status: row.pagamento_status,
          executado_em: row.executado_em,
          data_ativacao_ciclo: row.data_ativacao_ciclo,
        }));

        await exportRouteReport({
          route: "/tesouraria/refinanciamentos",
          format: exportFormat,
          rows,
          filters: {
            ...sourceQuery,
            ...describeReportScope(scope, referenceDate),
            totais: {
              total_registros: scopedRows.length,
              total_valor_renovacao: scopedRows
                .reduce(
                  (sum, row) =>
                    sum + Number.parseFloat(String(row.valor_liberado_associado ?? "0")),
                  0,
                )
                .toFixed(2),
              total_repasse_agente: scopedRows
                .reduce(
                  (sum, row) =>
                    sum + Number.parseFloat(String(row.repasse_agente ?? "0")),
                  0,
                )
                .toFixed(2),
            },
            columns: selectedColumns,
          },
        });
      } catch (error) {
        toast.error(
          error instanceof Error ? error.message : "Falha ao exportar renovações da tesouraria.",
        );
      } finally {
        setIsExporting(false);
      }
    },
    [cycleKey, dataFim, dataInicio, numeroCiclos, search],
  );

  return (
    <div className="space-y-6">
      <section className="rounded-[1.75rem] border border-border/60 bg-card/70 p-6">
        <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
          <div className="space-y-1">
            <p className="text-sm uppercase tracking-[0.28em] text-muted-foreground">
              Tesouraria
            </p>
            <h1 className="text-3xl font-semibold">Contratos para Renovação</h1>
            <p className="text-sm text-muted-foreground">
              Contratos aprovados pela coordenação, com fila operacional da tesouraria
              e histórico de efetivação.
            </p>
          </div>
          <ReportExportDialog
            disabled={isExporting}
            label="Exportar"
            showFilters
            agentOptions={agentOptions}
            statusOptions={[
              { value: "aprovado_para_renovacao", label: "Aguardando Pagamento" },
              { value: "efetivado", label: "Efetivado" },
              { value: "bloqueado", label: "Bloqueado" },
              { value: "revertido", label: "Revertido" },
              { value: "desativado", label: "Desativado" },
            ]}
            onExport={handleExport}
          />
        </div>
      </section>

      <section className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <StatsCard
          title="Pendentes para renovação"
          value={String(pendingQuery.data?.count ?? 0)}
          delta="Fila operacional aguardando anexos · clique para detalhar"
          icon={Clock3Icon}
          tone="neutral"
          tooltip="Renovações aprovadas pela coordenação aguardando comprovantes e efetivação na tesouraria."
          onClick={() =>
            setKpiModal({
              title: "Pendentes para renovação",
              statuses: PENDING_STATUS,
            })
          }
          active={kpiModal?.title === "Pendentes para renovação"}
        />
        <StatsCard
          title="Efetivadas"
          value={String(efetivadasQuery.data?.count ?? resumo?.efetivados ?? 0)}
          delta="Contratos concluídos pela tesouraria · clique para detalhar"
          icon={CheckCircle2Icon}
          tone="positive"
          tooltip="Renovações já pagas e ativadas no ciclo."
          onClick={() =>
            setKpiModal({
              title: "Efetivadas",
              statuses: EFETIVADO_STATUS,
            })
          }
          active={kpiModal?.title === "Efetivadas"}
        />
        <StatsCard
          title="Canceladas"
          value={String(canceladasTotal)}
          delta="Bloqueadas, revertidas ou desativadas · clique para detalhar"
          icon={XCircleIcon}
          tone="warning"
          tooltip="Renovações bloqueadas, revertidas ou desativadas no recorte atual."
          onClick={() =>
            setKpiModal({
              title: "Canceladas / Bloqueadas",
              statuses: CANCELADO_STATUS,
            })
          }
          active={kpiModal?.title === "Canceladas / Bloqueadas"}
        />
        <StatsCard
          title="Repasse total"
          value={formatCurrency(resumo?.repasse_total ?? "0")}
          delta="Soma do repasse no recorte atual"
          icon={HandCoinsIcon}
          tone="neutral"
          tooltip="Soma do repasse ao agente de todas as renovações efetivadas no recorte."
        />
      </section>

      <section className="grid gap-4 rounded-[1.75rem] border border-border/60 bg-card/70 p-5 lg:grid-cols-[minmax(0,1fr)_auto]">
        <div className="space-y-3">
          <div className="flex flex-wrap gap-2">
            <Button
              variant={rangeScope === "total" ? "default" : "outline"}
              className="h-10 rounded-2xl"
              onClick={() => {
                setRangeScope("total");
                setDataInicio(undefined);
                setDataFim(undefined);
              }}
            >
              Total
            </Button>
            <Button
              variant={rangeScope === "year" ? "default" : "outline"}
              className="h-10 rounded-2xl"
              onClick={() => {
                setRangeScope("year");
                setDataInicio(currentYearRange.start);
                setDataFim(currentYearRange.end);
              }}
            >
              Ano atual
            </Button>
          </div>
          <Input
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            placeholder="Buscar por associado, CPF, matrícula ou contrato..."
            className="h-11 rounded-2xl border-border/60 bg-background/60"
          />
        </div>
        <div className="flex justify-end">
          <Sheet
            open={filtersOpen}
            onOpenChange={(open) => {
              if (open) {
                setDraftRangeScope(rangeScope);
                setDraftDataInicio(dataInicio);
                setDraftDataFim(dataFim);
                setDraftCycleMonths(cycleMonths);
                setDraftCycleMonthPicker(undefined);
                setDraftNumeroCiclos(numeroCiclos);
              }
              setFiltersOpen(open);
            }}
          >
            <SheetTrigger asChild>
              <Button variant="outline" className="h-11 rounded-2xl">
                <SlidersHorizontalIcon className="size-4" />
                Filtros avançados
                {activeFiltersCount ? (
                  <Badge className="ml-1 rounded-full bg-primary/15 px-2 py-0 text-primary">
                    {activeFiltersCount}
                  </Badge>
                ) : null}
              </Button>
            </SheetTrigger>
            <SheetContent className="w-full border-l border-border/60 sm:max-w-xl">
              <SheetHeader>
                <SheetTitle>Filtros avançados</SheetTitle>
                <SheetDescription>
                  Refine o recorte da tesouraria por datas, ciclo e número do ciclo da renovação.
                </SheetDescription>
              </SheetHeader>

              <div className="space-y-5 overflow-y-auto px-4 pb-4">
                <div className="space-y-2">
                  <Label>Recorte rápido</Label>
                  <div className="flex gap-2">
                    <Button
                      type="button"
                      variant={draftRangeScope === "total" ? "default" : "outline"}
                      className="flex-1 rounded-xl"
                      onClick={() => {
                        setDraftRangeScope("total");
                        setDraftDataInicio(undefined);
                        setDraftDataFim(undefined);
                      }}
                    >
                      Total
                    </Button>
                    <Button
                      type="button"
                      variant={draftRangeScope === "year" ? "default" : "outline"}
                      className="flex-1 rounded-xl"
                      onClick={() => {
                        setDraftRangeScope("year");
                        setDraftDataInicio(currentYearRange.start);
                        setDraftDataFim(currentYearRange.end);
                      }}
                    >
                      Ano atual
                    </Button>
                  </div>
                </div>
                <div className="space-y-2">
                  <Label>Ciclo</Label>
                  <div className="space-y-3 rounded-2xl border border-border/60 bg-background/40 p-3">
                    <div className="flex flex-col gap-3 sm:flex-row sm:items-end">
                      <div className="min-w-0 flex-1">
                        <CalendarCompetencia
                          value={draftCycleMonthPicker}
                          onChange={setDraftCycleMonthPicker}
                        />
                      </div>
                      <Button
                        type="button"
                        variant="outline"
                        onClick={() => {
                          const nextMonth = formatMonthValue(draftCycleMonthPicker);
                          if (!nextMonth) {
                            return;
                          }
                          setDraftCycleMonths((current) =>
                            normalizeCycleMonths([...current, nextMonth]),
                          );
                          setDraftCycleMonthPicker(undefined);
                        }}
                      >
                        Adicionar mês
                      </Button>
                    </div>
                    {draftCycleMonths.length ? (
                      <div className="flex flex-wrap gap-2">
                        {draftCycleMonths.map((month) => (
                          <Badge
                            key={month}
                            className="cursor-pointer rounded-full bg-primary/15 px-3 py-1 text-primary"
                            onClick={() =>
                              setDraftCycleMonths((current) =>
                                current.filter((item) => item !== month),
                              )
                            }
                          >
                            {formatMonthYear(`${month}-01`)} ×
                          </Badge>
                        ))}
                      </div>
                    ) : (
                      <p className="text-xs text-muted-foreground">
                        Selecione os meses do ciclo um a um.
                      </p>
                    )}
                  </div>
                </div>
                <div className="space-y-2">
                  <Label htmlFor="filtro-numero-ciclos">Número do ciclo</Label>
                  <NativeSelect
                    id="filtro-numero-ciclos"
                    value={draftNumeroCiclos}
                    onChange={(event) => setDraftNumeroCiclos(event.target.value)}
                    className="h-11 rounded-xl border-border/60 bg-background/60"
                  >
                    <NativeSelectOption value="">Selecione</NativeSelectOption>
                    {NUMERO_CICLO_OPTIONS.map((option) => (
                      <NativeSelectOption key={option} value={option}>
                        Ciclo {option}
                      </NativeSelectOption>
                    ))}
                  </NativeSelect>
                </div>
                <div className="space-y-2">
                  <Label>Data inicial</Label>
                  <DatePicker
                    value={draftDataInicio}
                    onChange={(value) => {
                      setDraftRangeScope("custom");
                      setDraftDataInicio(value);
                    }}
                  />
                </div>
                <div className="space-y-2">
                  <Label>Data final</Label>
                  <DatePicker
                    value={draftDataFim}
                    onChange={(value) => {
                      setDraftRangeScope("custom");
                      setDraftDataFim(value);
                    }}
                  />
                </div>
              </div>

              <SheetFooter className="border-t border-border/60">
                <Button
                  variant="outline"
                  onClick={() => {
                    setDraftRangeScope("total");
                    setDraftDataInicio(undefined);
                    setDraftDataFim(undefined);
                    setDraftCycleMonths([]);
                    setDraftCycleMonthPicker(undefined);
                    setDraftNumeroCiclos("");
                    setRangeScope("total");
                    setDataInicio(undefined);
                    setDataFim(undefined);
                    setCycleMonths([]);
                    setNumeroCiclos("");
                    setFiltersOpen(false);
                  }}
                >
                  Limpar
                </Button>
                <Button
                  onClick={() => {
                    setRangeScope(draftRangeScope);
                    setDataInicio(draftDataInicio);
                    setDataFim(draftDataFim);
                    setCycleMonths(normalizeCycleMonths(draftCycleMonths));
                    setNumeroCiclos(draftNumeroCiclos);
                    setFiltersOpen(false);
                  }}
                >
                  Aplicar
                </Button>
              </SheetFooter>
            </SheetContent>
          </Sheet>
        </div>
      </section>

      <RefinanciamentoSection
        eyebrow="Pendentes"
        title="Contratos aprovados para pagamento"
        total={pendingQuery.data?.count ?? 0}
        page={pagePending}
        rows={pendingQuery.data?.results ?? []}
        columns={pendingColumns}
        onPageChange={setPagePending}
        loading={pendingQuery.isLoading}
        emptyMessage="Nenhuma renovação aprovada para renovação no recorte atual."
      />

      <RefinanciamentoSection
        eyebrow="Efetivadas"
        title="Contratos concluídos"
        total={efetivadasQuery.data?.count ?? 0}
        page={pageEfetivadas}
        rows={efetivadasQuery.data?.results ?? []}
        columns={historyColumns}
        onPageChange={setPageEfetivadas}
        loading={efetivadasQuery.isLoading}
        emptyMessage="Nenhuma renovação efetivada no recorte."
      />

      <RefinanciamentoSection
        eyebrow="Canceladas"
        title="Contratos bloqueados ou revertidos"
        total={canceladasQuery.data?.count ?? 0}
        page={pageCanceladas}
        rows={canceladasQuery.data?.results ?? []}
        columns={historyColumns}
        onPageChange={setPageCanceladas}
        loading={canceladasQuery.isLoading}
        emptyMessage="Nenhuma renovação cancelada no recorte."
      />

      {/* ─── Modal KPI ──────────────────────────────────────────────── */}
      <Dialog
        open={kpiModal !== null}
        onOpenChange={(open) => {
          if (!open) setKpiModal(null);
        }}
      >
        <DialogContent className="max-h-[85vh] max-w-5xl overflow-hidden p-0 flex flex-col">
          <DialogHeader className="px-6 pt-6 pb-4 border-b border-border/60">
            <DialogTitle>{kpiModal?.title ?? ""}</DialogTitle>
            <DialogDescription>
              {kpiModalQuery.data?.count ?? 0} registros no recorte atual
              {search ? ` · busca: "${search}"` : ""}
              {dataInicio || dataFim ? " · com filtro de data" : ""}
            </DialogDescription>
          </DialogHeader>
          <div className="flex-1 overflow-auto px-6 py-4">
            {kpiModalQuery.isLoading ? (
              <div className="space-y-3">
                {Array.from({ length: 5 }).map((_, i) => (
                  <Skeleton key={i} className="h-12 w-full rounded-xl" />
                ))}
              </div>
            ) : kpiModalQuery.data?.results.length === 0 ? (
              <p className="py-8 text-center text-sm text-muted-foreground">
                Nenhum registro encontrado para este filtro.
              </p>
            ) : (
              <Table>
                <TableHeader>
                  <TableRow>
                    {KPI_MODAL_COLUMNS.map((col) => (
                      <TableHead key={col.id}>{col.header}</TableHead>
                    ))}
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {(kpiModalQuery.data?.results ?? []).map((row) => (
                    <TableRow key={row.id}>
                      {KPI_MODAL_COLUMNS.map((col) => (
                        <TableCell key={col.id}>
                          {col.cell ? col.cell(row) : null}
                        </TableCell>
                      ))}
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
          </div>
          {(kpiModalQuery.data?.count ?? 0) > 20 && (
            <div className="flex items-center justify-between border-t border-border/60 px-6 py-3">
              <p className="text-sm text-muted-foreground">
                Página {kpiModalPage} de{" "}
                {Math.ceil((kpiModalQuery.data?.count ?? 0) / 20)}
              </p>
              <div className="flex gap-2">
                <Button
                  size="sm"
                  variant="outline"
                  disabled={kpiModalPage <= 1}
                  onClick={() => setKpiModalPage((p) => p - 1)}
                >
                  Anterior
                </Button>
                <Button
                  size="sm"
                  variant="outline"
                  disabled={
                    kpiModalPage >= Math.ceil((kpiModalQuery.data?.count ?? 0) / 20)
                  }
                  onClick={() => setKpiModalPage((p) => p + 1)}
                >
                  Próxima
                </Button>
              </div>
            </div>
          )}
        </DialogContent>
      </Dialog>

      <AssociadoDetailsDialog
        associadoId={detailAssociadoId}
        open={detailAssociadoId != null}
        onOpenChange={(open) => {
          if (!open) {
            setDetailAssociadoId(null);
          }
        }}
      />

      <Dialog
        open={efetivarTarget != null}
        onOpenChange={(open) => {
          if (!open && !efetivarMutation.isPending) setEfetivarTarget(null);
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Confirmar efetivação da renovação</DialogTitle>
            <DialogDescription>
              A renovação de <strong>{efetivarTarget?.associado_nome}</strong> será
              efetivada e um novo ciclo será gerado para o contrato. Selecione o
              tamanho do próximo ciclo. Essa escolha vale para o ciclo que será
              criado agora e para os próximos.
            </DialogDescription>
          </DialogHeader>
          <div className="flex flex-col gap-2 py-2">
            <label className="text-sm text-muted-foreground">
              Tamanho do próximo ciclo
            </label>
            <div className="flex gap-2">
              <Button
                type="button"
                variant={efetivarCicloParcelas === 3 ? "default" : "outline"}
                onClick={() => setEfetivarCicloParcelas(3)}
                disabled={efetivarMutation.isPending}
              >
                3 parcelas (padrão)
              </Button>
              <Button
                type="button"
                variant={efetivarCicloParcelas === 4 ? "default" : "outline"}
                onClick={() => setEfetivarCicloParcelas(4)}
                disabled={efetivarMutation.isPending}
              >
                4 parcelas
              </Button>
            </div>
          </div>
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => setEfetivarTarget(null)}
              disabled={efetivarMutation.isPending}
            >
              Cancelar
            </Button>
            <Button
              disabled={efetivarMutation.isPending}
              onClick={() =>
                efetivarTarget &&
                efetivarMutation.mutate({
                  refinanciamentoId: efetivarTarget.id,
                  proximoCicloParcelas: efetivarCicloParcelas,
                })
              }
            >
              Efetivar e gerar próximo ciclo
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={excluirTarget != null} onOpenChange={(open) => { if (!open) setExcluirTarget(null); }}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Remover renovação da fila</DialogTitle>
            <DialogDescription>
              A renovação de <strong>{excluirTarget?.associado_nome}</strong> será bloqueada e
              removida da fila operacional. O histórico será preservado.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setExcluirTarget(null)}>
              Cancelar
            </Button>
            <Button
              variant="destructive"
              disabled={excluirMutation.isPending}
              onClick={() => excluirTarget && excluirMutation.mutate(excluirTarget.id)}
            >
              Remover da fila
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog
        open={retornarTarget != null}
        onOpenChange={(open) => {
          if (!open) setRetornarTarget(null);
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Voltar para pendente de pagamento</DialogTitle>
            <DialogDescription>
              A renovação de <strong>{retornarTarget?.associado_nome}</strong> voltará para a
              fila de pagamento da tesouraria, preservando os anexos já existentes.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setRetornarTarget(null)}>
              Cancelar
            </Button>
            <Button
              disabled={retornarPendenteMutation.isPending}
              onClick={() =>
                retornarTarget && retornarPendenteMutation.mutate(retornarTarget.id)
              }
            >
              Voltar para pendente
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog
        open={limparLinhaTarget != null}
        onOpenChange={(open) => {
          if (!open) setLimparLinhaTarget(null);
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Limpar linha operacional</DialogTitle>
            <DialogDescription>
              A linha de <strong>{limparLinhaTarget?.associado_nome}</strong> será removida da
              esteira de renovação. Use isso apenas para registros incorretos.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setLimparLinhaTarget(null)}>
              Cancelar
            </Button>
            <Button
              variant="destructive"
              disabled={limparLinhaMutation.isPending}
              onClick={() =>
                limparLinhaTarget && limparLinhaMutation.mutate(limparLinhaTarget.id)
              }
            >
              Limpar linha
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

function RefinanciamentoSection({
  eyebrow,
  title,
  total,
  page,
  rows,
  columns,
  onPageChange,
  loading,
  emptyMessage,
}: {
  eyebrow: string;
  title: string;
  total: number;
  page: number;
  rows: RefinanciamentoItem[];
  columns: DataTableColumn<RefinanciamentoItem>[];
  onPageChange: (page: number) => void;
  loading: boolean;
  emptyMessage: string;
}) {
  const start = total ? (page - 1) * 15 + 1 : 0;
  const end = total ? start + rows.length - 1 : 0;

  return (
    <section className="space-y-4">
      <header className="flex items-center justify-between">
        <div>
          <p className="text-xs uppercase tracking-[0.28em] text-muted-foreground">{eyebrow}</p>
          <h2 className="text-xl font-semibold">{title}</h2>
        </div>
        <p className="text-sm text-muted-foreground">
          Mostrando {total ? `${start}-${end} de ${total}` : "0"}
        </p>
      </header>
      <DataTable
        data={rows}
        columns={columns}
        currentPage={page}
        totalPages={Math.max(1, Math.ceil(total / 15))}
        onPageChange={onPageChange}
        emptyMessage={emptyMessage}
        loading={loading}
        skeletonRows={6}
      />
    </section>
  );
}
