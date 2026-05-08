"use client";

import * as React from "react";
import { format } from "date-fns";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  CalendarDaysIcon,
  CheckCircle2Icon,
  Clock3Icon,
  EyeIcon,
  HandCoinsIcon,
  LayoutGridIcon,
  SlidersHorizontalIcon,
  PaperclipIcon,
  LockIcon,
  Trash2Icon,
  UploadIcon,
  XCircleIcon,
} from "lucide-react";
import { toast } from "sonner";

import type {
  PaginatedResponse,
  SimpleUser,
  TesourariaContratoItem,
} from "@/lib/api/types";
import { apiFetch } from "@/lib/api/client";
import { buildBackendFileUrl } from "@/lib/backend-files";
import { formatCurrency, formatDateTime } from "@/lib/formatters";
import { maskCPFCNPJ } from "@/lib/masks";
import {
  describeReportScope,
  exportRouteReport,
  fetchAllPaginatedRows,
  filterRowsByReportScope,
} from "@/lib/reports";
import DatePicker from "@/components/custom/date-picker";
import SearchableSelect from "@/components/custom/searchable-select";
import StatusBadge from "@/components/custom/status-badge";
import AssociadoDetailsDialog from "@/components/associados/associado-details-dialog";
import CopySnippet from "@/components/shared/copy-snippet";
import DataTable, {
  type DataTableColumn,
} from "@/components/shared/data-table";
import ReportExportDialog, {
  type ReportExportFilters,
} from "@/components/shared/report-export-dialog";
import StatsCard from "@/components/shared/stats-card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Sheet,
  SheetClose,
  SheetContent,
  SheetDescription,
  SheetFooter,
  SheetHeader,
  SheetTitle,
  SheetTrigger,
} from "@/components/ui/sheet";
import { Textarea } from "@/components/ui/textarea";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { usePermissions } from "@/hooks/use-permissions";

const comprovanteAccept = {
  "application/pdf": [".pdf"],
  "image/png": [".png"],
  "image/jpeg": [".jpg", ".jpeg"],
};

const PAGE_SIZE = 5;

type TesourariaPagamentoFilter =
  | "pendente"
  | "concluido"
  | "liquidado"
  | "cancelado"
  | "processado";
type TesourariaSectionFilter =
  | "todos"
  | "pendente"
  | "reativacao"
  | "concluido"
  | "liquidado"
  | "cancelado";
type AdvancedFiltersDraft = {
  dataInicio?: Date;
  dataFim?: Date;
  agente: string;
  statusContrato: string;
  situacaoEsteira: string;
  ordering: string;
};
type TesourariaAgentFilterUser = SimpleUser & {
  email?: string;
  primary_role?: string | null;
};

function toStatusLabel(value: string) {
  return value
    .split("_")
    .filter(Boolean)
    .map((chunk) => chunk.charAt(0).toUpperCase() + chunk.slice(1))
    .join(" ");
}

function formatTesourariaAnexos(row: TesourariaContratoItem) {
  const associado = row.comprovantes.find((item) => item.papel === "associado");
  const agente = row.comprovantes.find((item) => item.papel === "agente");

  const formatItem = (
    label: string,
    item?: TesourariaContratoItem["comprovantes"][number],
  ) => {
    if (!item) {
      return `${label}: sem anexo`;
    }
    if (item.nome_original) {
      return `${label}: ${item.nome_original}`;
    }
    if (item.arquivo_referencia) {
      return `${label}: ${item.arquivo_referencia}`;
    }
    return `${label}: anexado`;
  };

  return [
    formatItem("Associado", associado),
    formatItem("Agente", agente),
  ].join(" | ");
}

function formatTesourariaDadosBancarios(row: TesourariaContratoItem) {
  if (!row.dados_bancarios) {
    return "Sem dados bancários";
  }
  const { banco, agencia, conta, tipo_conta } = row.dados_bancarios;
  return [banco, `Ag ${agencia}`, `Conta ${conta}`, tipo_conta]
    .filter(Boolean)
    .join(" | ");
}

function hasTesourariaPaymentProofs(row: TesourariaContratoItem) {
  const hasAssociado = row.comprovantes.some((item) => item.papel === "associado");
  const hasAgente = row.comprovantes.some((item) => item.papel === "agente");
  return hasAssociado && hasAgente;
}

function getOperationalDeleteCopy(row: TesourariaContratoItem) {
  if (row.status === "cancelado") {
    return {
      title: "Remover da fila preservando histórico",
      description:
        "O contrato não será apagado. O item operacional será reclassificado conforme o status real de cancelamento.",
      confirmLabel: "Remover da fila",
    };
  }

  if (row.status === "concluido" || row.status === "liquidado") {
    return {
      title: "Remover da fila preservando histórico",
      description:
        "O associado, o contrato e o financeiro serão preservados. Apenas o item operacional sairá da fila incorreta.",
      confirmLabel: "Remover da fila",
    };
  }

  return {
    title: "Remover da fila preservando histórico",
    description:
      "O associado, documentos e histórico serão preservados. Apenas a linha operacional deixará de aparecer na fila.",
    confirmLabel: "Remover da fila",
  };
}

function formatTesourariaAcoes(row: TesourariaContratoItem) {
  const actions = ["Ver detalhes"];
  if ((row.status === "pendente" || row.status === "congelado") && !row.dispensa_pagamento_inicial) {
    actions.push(
      hasTesourariaPaymentProofs(row) ? "Efetivar" : "Aguardando comprovantes",
    );
  }
  if (row.status === "pendente" || row.status === "congelado") {
    actions.push("Congelar", "Pendenciar para análise", "Cancelar contrato");
  }
  actions.push(
    row.status === "pendente" || row.status === "congelado"
      ? "Remover da fila"
      : "Remover da fila",
  );
  if (row.status === "cancelado" && row.cancelamento_tipo === "desistente") {
    return [...actions, "Desistente"].join(" | ");
  }
  return actions.join(" | ");
}

function formatTesourariaAgente(row: TesourariaContratoItem) {
  return [
    row.agente_nome || "Sem agente",
    `Repasse: ${row.percentual_repasse}%`,
  ].join(" | ");
}

function formatTesourariaValores(row: TesourariaContratoItem) {
  return [
    `Aux. liberado: ${formatCurrency(row.margem_disponivel)}`,
    `Comissão: ${formatCurrency(row.comissao_agente)}`,
  ].join(" | ");
}

function formatTesourariaStatus(row: TesourariaContratoItem) {
  const parts = [toStatusLabel(row.status)];
  if (row.origem_operacional === "reativacao") {
    parts.push(row.origem_operacional_label || "Reativação");
  }
  if (row.cancelamento_tipo) {
    parts.push(
      row.cancelamento_tipo === "desistente" ? "Desistente" : "Cancelado",
    );
  }
  return parts.join(" | ");
}

function formatCompetenciaLabel(value: string) {
  const [year, month] = value.split("-").map((part) => Number(part));
  if (!year || !month) {
    return value;
  }
  return format(new Date(year, month - 1, 1), "MM/yyyy");
}

function buildTesourariaExportRow(row: TesourariaContratoItem) {
  return {
    anexos: formatTesourariaAnexos(row),
    dados_bancarios: formatTesourariaDadosBancarios(row),
    chave_pix: row.chave_pix || "—",
    acao: formatTesourariaAcoes(row),
    nome: row.nome,
    matricula_cpf: `${row.matricula || "—"} | ${maskCPFCNPJ(row.cpf_cnpj)}`,
    agente: formatTesourariaAgente(row),
    auxilio_comissao: formatTesourariaValores(row),
    data_solicitacao: formatDateTime(row.data_solicitacao),
    data_anexo_associado: formatDateTime(row.data_anexo_associado),
    data_anexo_agente: formatDateTime(row.data_anexo_agente),
    data_pagamento_associado: formatDateTime(row.data_pagamento_associado),
    data_pagamento_agente: formatDateTime(row.data_pagamento_agente),
    status: formatTesourariaStatus(row),
  };
}

function toFilterDate(value?: Date) {
  return value ? format(value, "yyyy-MM-dd") : undefined;
}

function countAdvancedFilters(filters: AdvancedFiltersDraft) {
  return [
    Boolean(filters.dataInicio),
    Boolean(filters.dataFim),
    Boolean(filters.agente),
    filters.statusContrato !== "todos",
    filters.situacaoEsteira !== "todos",
    filters.ordering !== "-created_at",
  ].filter(Boolean).length;
}

function formatActiveFilterScope({
  dataInicio,
  dataFim,
  hasActiveFilters,
}: {
  dataInicio?: Date;
  dataFim?: Date;
  hasActiveFilters: boolean;
}) {
  if (dataInicio && dataFim) {
    return `${format(dataInicio, "dd/MM/yy")} a ${format(dataFim, "dd/MM/yy")}`;
  }
  if (dataInicio) {
    return `A partir de ${format(dataInicio, "dd/MM/yy")}`;
  }
  if (dataFim) {
    return `Até ${format(dataFim, "dd/MM/yy")}`;
  }
  return hasActiveFilters ? "Filtro ativo" : "Sem recorte";
}

function useTesourariaQuery({
  page,
  search,
  pagamento,
  origemOperacional,
  dataInicio,
  dataFim,
  agente,
  statusContrato,
  situacaoEsteira,
  ordering,
}: {
  page: number;
  search: string;
  pagamento: TesourariaPagamentoFilter;
  origemOperacional?: string;
  dataInicio?: Date;
  dataFim?: Date;
  agente: string;
  statusContrato: string;
  situacaoEsteira: string;
  ordering: string;
}) {
  return useQuery({
    queryKey: [
      "tesouraria-contratos",
      pagamento,
      origemOperacional,
      page,
      search,
      dataInicio?.toISOString(),
      dataFim?.toISOString(),
      agente,
      statusContrato,
      situacaoEsteira,
      ordering,
    ],
    queryFn: () =>
      apiFetch<PaginatedResponse<TesourariaContratoItem>>(
        "tesouraria/contratos",
        {
          query: {
            page,
            page_size: PAGE_SIZE,
            pagamento,
            origem_operacional: origemOperacional || undefined,
            search: search || undefined,
            data_inicio: toFilterDate(dataInicio),
            data_fim: toFilterDate(dataFim),
            agente: agente || undefined,
            status_contrato:
              statusContrato === "todos" ? undefined : statusContrato,
            situacao_esteira:
              situacaoEsteira === "todos" ? undefined : situacaoEsteira,
            ordering,
          },
        },
      ),
  });
}

export default function TesourariaPage() {
  const { hasAnyRole } = usePermissions();
  const canMutate = hasAnyRole(["ADMIN", "TESOUREIRO"]);
  const canOperationalDelete = hasAnyRole(["ADMIN", "COORDENADOR"]);
  const queryClient = useQueryClient();
  const [search, setSearch] = React.useState("");
  const [isExporting, setIsExporting] = React.useState(false);
  const [pagePending, setPagePending] = React.useState(1);
  const [pageReactivation, setPageReactivation] = React.useState(1);
  const [pagePaid, setPagePaid] = React.useState(1);
  const [pageLiquidated, setPageLiquidated] = React.useState(1);
  const [pageCanceled, setPageCanceled] = React.useState(1);
  const [visibleSection, setVisibleSection] =
    React.useState<TesourariaSectionFilter>("todos");
  const [dataInicio, setDataInicio] = React.useState<Date | undefined>();
  const [dataFim, setDataFim] = React.useState<Date | undefined>();
  const [agenteFiltro, setAgenteFiltro] = React.useState("");
  const [statusContrato, setStatusContrato] = React.useState("todos");
  const [situacaoEsteira, setSituacaoEsteira] = React.useState("todos");
  const [ordering, setOrdering] = React.useState("-created_at");
  const [draftAdvancedFilters, setDraftAdvancedFilters] =
    React.useState<AdvancedFiltersDraft>({
      dataInicio: undefined,
      dataFim: undefined,
      agente: "",
      statusContrato: "todos",
      situacaoEsteira: "todos",
      ordering: "-created_at",
    });
  const [freezeTarget, setFreezeTarget] =
    React.useState<TesourariaContratoItem | null>(null);
  const [freezeReason, setFreezeReason] = React.useState("");
  const [pendenciarTarget, setPendenciarTarget] =
    React.useState<TesourariaContratoItem | null>(null);
  const [pendenciaDescricao, setPendenciaDescricao] = React.useState("");
  const [cancelTarget, setCancelTarget] =
    React.useState<TesourariaContratoItem | null>(null);
  const [cancelReason, setCancelReason] = React.useState("");
  const [cancelType, setCancelType] = React.useState<
    "cancelado" | "desistente"
  >("cancelado");
  const [bankTarget, setBankTarget] =
    React.useState<TesourariaContratoItem | null>(null);
  const [detailTarget, setDetailTarget] =
    React.useState<TesourariaContratoItem | null>(null);
  const [deleteTarget, setDeleteTarget] =
    React.useState<TesourariaContratoItem | null>(null);
  const [effectivationTarget, setEffectivationTarget] =
    React.useState<TesourariaContratoItem | null>(null);
  const agentesFiltroQuery = useQuery({
    queryKey: ["tesouraria-contratos-agentes"],
    queryFn: () =>
      apiFetch<TesourariaAgentFilterUser[]>("tesouraria/contratos/agentes"),
    staleTime: 5 * 60 * 1000,
  });

  const pendingQuery = useTesourariaQuery({
    page: pagePending,
    search,
    pagamento: "pendente",
    origemOperacional: "cadastro",
    dataInicio,
    dataFim,
    agente: agenteFiltro,
    statusContrato,
    situacaoEsteira,
    ordering,
  });
  const reactivationQuery = useTesourariaQuery({
    page: pageReactivation,
    search,
    pagamento: "pendente",
    origemOperacional: "reativacao",
    dataInicio,
    dataFim,
    agente: agenteFiltro,
    statusContrato,
    situacaoEsteira,
    ordering,
  });
  const paidQuery = useTesourariaQuery({
    page: pagePaid,
    search,
    pagamento: "concluido",
    dataInicio,
    dataFim,
    agente: agenteFiltro,
    statusContrato,
    situacaoEsteira,
    ordering,
  });
  const liquidatedQuery = useTesourariaQuery({
    page: pageLiquidated,
    search,
    pagamento: "liquidado",
    dataInicio,
    dataFim,
    agente: agenteFiltro,
    statusContrato,
    situacaoEsteira,
    ordering,
  });
  const canceledQuery = useTesourariaQuery({
    page: pageCanceled,
    search,
    pagamento: "cancelado",
    dataInicio,
    dataFim,
    agente: agenteFiltro,
    statusContrato,
    situacaoEsteira,
    ordering,
  });

  const congelarMutation = useMutation({
    mutationFn: async ({
      contratoId,
      motivo,
    }: {
      contratoId: number;
      motivo: string;
    }) =>
      apiFetch<TesourariaContratoItem>(
        `tesouraria/contratos/${contratoId}/congelar`,
        {
          method: "POST",
          body: { motivo },
        },
      ),
    onSuccess: () => {
      toast.success("Contrato congelado.");
      setFreezeTarget(null);
      setFreezeReason("");
      void queryClient.invalidateQueries({
        queryKey: ["tesouraria-contratos"],
      });
    },
    onError: (error) => {
      toast.error(
        error instanceof Error ? error.message : "Falha ao congelar contrato.",
      );
    },
  });

  const pendenciarMutation = useMutation({
    mutationFn: async ({
      contratoId,
      descricao,
    }: {
      contratoId: number;
      descricao: string;
    }) =>
      apiFetch<TesourariaContratoItem>(
        `tesouraria/contratos/${contratoId}/pendenciar`,
        {
          method: "POST",
          body: {
            tipo: "tesouraria",
            descricao,
          },
        },
      ),
    onSuccess: () => {
      toast.success("Contrato retornado da tesouraria para a análise.");
      setPendenciarTarget(null);
      setPendenciaDescricao("");
      void queryClient.invalidateQueries({
        queryKey: ["tesouraria-contratos"],
      });
      void queryClient.invalidateQueries({
        queryKey: ["analise-resumo"],
      });
      void queryClient.invalidateQueries({
        queryKey: ["analise-filas"],
      });
      void queryClient.invalidateQueries({
        queryKey: ["dashboard-esteira"],
      });
    },
    onError: (error) => {
      toast.error(
        error instanceof Error
          ? error.message
          : "Falha ao devolver contrato para a análise.",
      );
    },
  });

  const averbarMutation = useMutation({
    mutationFn: async ({ contratoId }: { contratoId: number }) =>
      apiFetch<TesourariaContratoItem>(
        `tesouraria/contratos/${contratoId}/averbar`,
        {
          method: "POST",
        },
      ),
    onSuccess: () => {
      toast.success("Contrato averbado com sucesso.");
      void queryClient.invalidateQueries({
        queryKey: ["tesouraria-contratos"],
      });
      void queryClient.invalidateQueries({
        queryKey: ["tesouraria-pagamentos"],
      });
    },
    onError: (error) => {
      toast.error(
        error instanceof Error ? error.message : "Falha ao averbar contrato.",
      );
    },
  });

  const substituirComprovanteMutation = useMutation({
    mutationFn: async ({
      contratoId,
      papel,
      arquivo,
    }: {
      contratoId: number;
      papel: "associado" | "agente";
      arquivo: File;
    }) => {
      const formData = new FormData();
      formData.set("papel", papel);
      formData.set("arquivo", arquivo);
      return apiFetch<TesourariaContratoItem>(
        `tesouraria/contratos/${contratoId}/substituir-comprovante`,
        {
          method: "POST",
          formData,
        },
      );
    },
    onSuccess: () => {
      toast.success("Nova versão do comprovante adicionada.");
      void queryClient.invalidateQueries({
        queryKey: ["tesouraria-contratos"],
      });
    },
    onError: (error) => {
      toast.error(
        error instanceof Error
          ? error.message
          : "Falha ao adicionar nova versão do comprovante.",
      );
    },
  });

  const efetivarMutation = useMutation({
    mutationFn: async ({
      contratoId,
      competenciasCiclo,
    }: {
      contratoId: number;
      competenciasCiclo?: string[];
    }) =>
      apiFetch<TesourariaContratoItem>(
        `tesouraria/contratos/${contratoId}/efetivar`,
        {
          method: "POST",
          body: competenciasCiclo
            ? { competencias_ciclo: competenciasCiclo }
            : {},
        },
      ),
    onSuccess: () => {
      toast.success("Contrato efetivado com sucesso.");
      setEffectivationTarget(null);
      void queryClient.invalidateQueries({
        queryKey: ["tesouraria-contratos"],
      });
      void queryClient.invalidateQueries({
        queryKey: ["tesouraria-pagamentos"],
      });
    },
    onError: (error) => {
      toast.error(
        error instanceof Error ? error.message : "Falha ao efetivar contrato.",
      );
    },
  });

  const excluirMutation = useMutation({
    mutationFn: async ({ contratoId }: { contratoId: number }) =>
      apiFetch<TesourariaContratoItem>(
        `tesouraria/contratos/${contratoId}/excluir`,
        {
          method: "POST",
          body: {},
        },
      ),
    onSuccess: (_, variables) => {
      const target = deleteTarget;
      toast.success(
        "Item removido da fila com histórico preservado.",
      );
      setDeleteTarget(null);
      void queryClient.invalidateQueries({
        queryKey: ["tesouraria-contratos"],
      });
      void queryClient.invalidateQueries({ queryKey: ["associados"] });
      void queryClient.invalidateQueries({ queryKey: ["analise"] });
      void queryClient.invalidateQueries({ queryKey: ["contratos"] });
    },
    onError: (error) => {
      toast.error(
        error instanceof Error
          ? error.message
          : "Falha ao excluir item operacional.",
      );
    },
  });

  const cancelarMutation = useMutation({
    mutationFn: async ({
      contratoId,
      tipo,
      motivo,
    }: {
      contratoId: number;
      tipo: "cancelado" | "desistente";
      motivo: string;
    }) =>
      apiFetch<TesourariaContratoItem>(
        `tesouraria/contratos/${contratoId}/cancelar`,
        {
          method: "POST",
          body: { tipo, motivo },
        },
      ),
    onSuccess: () => {
      toast.success("Contrato cancelado com sucesso.");
      setCancelTarget(null);
      setCancelReason("");
      setCancelType("cancelado");
      void queryClient.invalidateQueries({
        queryKey: ["tesouraria-contratos"],
      });
      void queryClient.invalidateQueries({ queryKey: ["contratos"] });
    },
    onError: (error) => {
      toast.error(
        error instanceof Error ? error.message : "Falha ao cancelar contrato.",
      );
    },
  });

  const columns = React.useMemo<DataTableColumn<TesourariaContratoItem>[]>(
    () => [
      {
        id: "anexos",
        header: "Anexos",
        cell: (row) => {
          const associadoComprovante = row.comprovantes.find(
            (item) => item.papel === "associado",
          );
          const agenteComprovante = row.comprovantes.find(
            (item) => item.papel === "agente",
          );
          const canEfetivar =
            row.status === "pendente" || row.status === "congelado";
          const hasAllProofs = hasTesourariaPaymentProofs(row);
          const dispensaPagamentoInicial = row.dispensa_pagamento_inicial;
          const isAverbando =
            averbarMutation.isPending &&
            averbarMutation.variables?.contratoId === row.id;
          const isSubstituindo =
            substituirComprovanteMutation.isPending &&
            substituirComprovanteMutation.variables?.contratoId === row.id;
          const isEfetivando =
            efetivarMutation.isPending &&
            efetivarMutation.variables?.contratoId === row.id;
          const isBlocked = isAverbando || isSubstituindo || isEfetivando;

          if (dispensaPagamentoInicial) {
            return (
              <div className="flex min-w-[13rem] flex-col gap-3 rounded-2xl border border-dashed border-border/60 bg-card/40 p-3">
                <p className="text-sm font-medium">
                  Fluxo legado sem mensalidade
                </p>
                <p className="text-xs text-muted-foreground">
                  Este contrato já existe como exceção legada com mensalidade
                  zerada. Esse fluxo não fica mais disponível para novos
                  cadastros.
                </p>
                {canEfetivar ? (
                  <Button
                    size="sm"
                    className="self-start"
                    onClick={() =>
                      averbarMutation.mutate({ contratoId: row.id })
                    }
                    disabled={isAverbando || !canMutate}
                  >
                    Averbar
                  </Button>
                ) : (
                  <Badge variant="outline" className="w-fit rounded-full">
                    Averbado
                  </Badge>
                )}
              </div>
            );
          }

          return (
            <div className="flex min-w-[13rem] flex-col gap-2">
              <div className="grid gap-2">
                <ComprovanteSlot
                  compact
                  label="Associado"
                  existingUrl={
                    associadoComprovante?.arquivo_disponivel_localmente
                      ? associadoComprovante.arquivo
                      : undefined
                  }
                  existingName={associadoComprovante?.nome_original}
                  disabled={isBlocked || !canMutate}
                  isProcessing={isBlocked}
                  onSelect={(file) => {
                    substituirComprovanteMutation.mutate({
                      contratoId: row.id,
                      papel: "associado",
                      arquivo: file,
                    });
                  }}
                  onClear={() => undefined}
                />
                <ComprovanteSlot
                  compact
                  label="Agente"
                  existingUrl={
                    agenteComprovante?.arquivo_disponivel_localmente
                      ? agenteComprovante.arquivo
                      : undefined
                  }
                  existingName={agenteComprovante?.nome_original}
                  disabled={isBlocked || !canMutate}
                  isProcessing={isBlocked}
                  onSelect={(file) => {
                    substituirComprovanteMutation.mutate({
                      contratoId: row.id,
                      papel: "agente",
                      arquivo: file,
                    });
                  }}
                  onClear={() => undefined}
                />
              </div>
              <p className="text-xs text-muted-foreground">
                {canEfetivar
                  ? hasAllProofs
                    ? "Os dois comprovantes já estão anexados. Clique em Efetivar para concluir."
                    : "A efetivação exige comprovante do associado e do agente. O upload isolado não conclui a etapa."
                  : "Novos comprovantes são adicionados ao histórico sem apagar versões anteriores."}
              </p>
            </div>
          );
        },
      },
      {
        id: "dados_bancarios",
        header: "Dados bancários",
        cell: (row) => (
          <Button size="sm" onClick={() => setBankTarget(row)}>
            <CalendarDaysIcon className="size-4" />
            Ver dados
          </Button>
        ),
      },
      {
        id: "pix",
        header: "Chave PIX",
        cell: (row) =>
          row.chave_pix ? (
            <CopySnippet label="PIX" value={row.chave_pix} inline />
          ) : (
            "—"
          ),
      },
      {
        id: "acao",
        header: "Ação",
        cell: (row) => {
          const canFreeze =
            row.status === "pendente" || row.status === "congelado";
          const canCancel =
            row.status === "pendente" || row.status === "congelado";
          const canPendenciar =
            row.status === "pendente" || row.status === "congelado";
          const canEfetivar =
            (row.status === "pendente" || row.status === "congelado") &&
            !row.dispensa_pagamento_inicial;
          const hasAllProofs = hasTesourariaPaymentProofs(row);
          const isEfetivando =
            efetivarMutation.isPending &&
            efetivarMutation.variables?.contratoId === row.id;
          const deleteCopy = getOperationalDeleteCopy(row);

          return (
            <div className="flex min-w-52 flex-wrap gap-2">
              <Button
                size="sm"
                variant="outline"
                onClick={() => setDetailTarget(row)}
              >
                <EyeIcon className="size-4" />
                Ver detalhes
              </Button>
              <Button
                size="sm"
                variant="outline"
                className="border-emerald-500/40 text-emerald-200"
                onClick={() => {
                  if (row.origem_operacional === "reativacao") {
                    setEffectivationTarget(row);
                    return;
                  }
                  efetivarMutation.mutate({ contratoId: row.id });
                }}
                disabled={!canEfetivar || !hasAllProofs || !canMutate || isEfetivando}
              >
                <CheckCircle2Icon className="size-4" />
                Efetivar
              </Button>
              <Button
                size="sm"
                variant="outline"
                className="border-amber-500/40 text-amber-200"
                onClick={() => setFreezeTarget(row)}
                disabled={!canFreeze || !canMutate}
              >
                <LockIcon className="size-4" />
                Congelar
              </Button>
              <Button
                size="sm"
                variant="outline"
                className="border-sky-500/40 text-sky-200"
                onClick={() => setPendenciarTarget(row)}
                disabled={!canPendenciar || !canMutate}
              >
                Pendenciar para análise
              </Button>
              <Button
                size="sm"
                variant="outline"
                className="border-rose-500/40 text-rose-200"
                onClick={() => setCancelTarget(row)}
                disabled={!canCancel || !canMutate}
              >
                Cancelar contrato
              </Button>
              <Button
                size="sm"
                variant="outline"
                className="border-rose-500/40 text-rose-200"
                onClick={() => setDeleteTarget(row)}
                disabled={!canOperationalDelete}
              >
                <Trash2Icon className="size-4" />
                {deleteCopy.confirmLabel}
              </Button>
            </div>
          );
        },
      },
      {
        id: "nome",
        header: "Nome",
        cell: (row) => (
          <div className="space-y-1">
            <p className="font-medium">{row.nome}</p>
            {row.origem_operacional === "reativacao" ? (
              <Badge className="rounded-full bg-emerald-500/15 text-emerald-200">
                {row.origem_operacional_label || "Reativação"}
              </Badge>
            ) : null}
          </div>
        ),
      },
      {
        id: "matricula_cpf",
        header: "Matrícula / CPF",
        cell: (row) => (
          <div className="space-y-1">
            <div>
              {row.matricula ? (
                <CopySnippet
                  label="Matrícula"
                  value={row.matricula}
                  mono
                  inline
                />
              ) : (
                <span className="text-muted-foreground">—</span>
              )}
            </div>
            <CopySnippet
              label="CPF"
              value={maskCPFCNPJ(row.cpf_cnpj)}
              copyValue={row.cpf_cnpj}
              mono
              inline
            />
          </div>
        ),
      },
      {
        id: "agente",
        header: "Agente",
        cell: (row) => (
          <div>
            <p className="font-medium">{row.agente_nome || "Sem agente"}</p>
            <p className="text-xs text-muted-foreground">
              Repasse: {row.percentual_repasse}%
            </p>
          </div>
        ),
      },
      {
        id: "valores",
        header: "Auxílio liberado / comissão",
        cell: (row) => (
          <div className="space-y-1">
            <div className="flex items-center justify-between gap-3 text-sm">
              <span className="text-muted-foreground">Aux. liberado</span>
              <span className="font-medium">
                {formatCurrency(row.margem_disponivel)}
              </span>
            </div>
            <div className="flex items-center justify-between gap-3 text-sm">
              <span className="text-muted-foreground">Comissão</span>
              <span className="font-medium">
                {formatCurrency(row.comissao_agente)}
              </span>
            </div>
          </div>
        ),
      },
      {
        id: "data",
        header: "Data da solicitação",
        cell: (row) => formatDateTime(row.data_solicitacao),
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
          <div className="space-y-1">
            <StatusBadge status={row.status} />
            {row.origem_operacional === "reativacao" ? (
              <Badge className="rounded-full bg-emerald-500/15 text-emerald-200">
                {row.origem_operacional_label || "Reativação"}
              </Badge>
            ) : null}
            {row.cancelamento_tipo ? (
              <Badge className="rounded-full bg-rose-500/15 text-rose-200">
                {row.cancelamento_tipo === "desistente"
                  ? "Desistente"
                  : "Cancelado"}
              </Badge>
            ) : null}
          </div>
        ),
      },
    ],
    [
      averbarMutation,
      canMutate,
      canOperationalDelete,
      efetivarMutation,
      substituirComprovanteMutation,
    ],
  );

  const handleExport = React.useCallback(
    async (exportFilters: ReportExportFilters, formatValue: "pdf" | "xlsx") => {
      const {
        scope,
        referenceDate,
        agente: exportAgente,
        status: exportStatus,
        esteira: exportEsteira,
        origemOperacional: exportOrigemOperacional,
        columns: selectedColumns,
      } = exportFilters;

      setIsExporting(true);
      try {
        const sources =
          visibleSection === "todos"
            ? ([
                { pagamento: "pendente" as const },
                { pagamento: "concluido" as const },
                { pagamento: "liquidado" as const },
                { pagamento: "cancelado" as const },
              ] as const)
            : visibleSection === "pendente" || visibleSection === "reativacao"
              ? ([{ pagamento: "pendente" as const }] as const)
              : ([{ pagamento: visibleSection }] as const);
        const origemOperacional =
          exportOrigemOperacional ||
          (visibleSection === "pendente"
            ? "cadastro"
            : visibleSection === "reativacao"
              ? "reativacao"
              : undefined);
        const sharedQuery = {
          search: search || undefined,
          data_inicio: toFilterDate(dataInicio),
          data_fim: toFilterDate(dataFim),
          agente: exportAgente || agenteFiltro || undefined,
          status_contrato:
            exportStatus ||
            (statusContrato !== "todos" ? statusContrato : undefined),
          situacao_esteira:
            exportEsteira ||
            (situacaoEsteira !== "todos" ? situacaoEsteira : undefined),
          origem_operacional: origemOperacional,
          ordering,
        };

        const fetchedRows = (
          await Promise.all(
            sources.map((source) =>
              fetchAllPaginatedRows<TesourariaContratoItem>({
                sourcePath: "tesouraria/contratos",
                sourceQuery: {
                  ...sharedQuery,
                  pagamento: source.pagamento,
                },
              }),
            ),
          )
        ).flat();

        const exportRows = filterRowsByReportScope({
          rows: fetchedRows,
          scope,
          referenceDate,
          getCandidates: (row) => [row.data_solicitacao, row.data_assinatura],
        }).map((row) => buildTesourariaExportRow(row));
        const scopedRows = filterRowsByReportScope({
          rows: fetchedRows,
          scope,
          referenceDate,
          getCandidates: (row) => [row.data_solicitacao, row.data_assinatura],
        });

        await exportRouteReport({
          route: "/tesouraria",
          format: formatValue,
          rows: exportRows,
          filters: {
            ...sharedQuery,
            pagamento: sources.map((source) => source.pagamento),
            totais: {
              total_registros: scopedRows.length,
              total_auxilio_liberado: scopedRows
                .reduce(
                  (total, row) =>
                    total +
                    Number.parseFloat(String(row.margem_disponivel ?? "0")),
                  0,
                )
                .toFixed(2),
              total_comissao_agente: scopedRows
                .reduce(
                  (total, row) =>
                    total +
                    Number.parseFloat(String(row.comissao_agente ?? "0")),
                  0,
                )
                .toFixed(2),
              total_mensalidade: scopedRows
                .reduce(
                  (total, row) =>
                    total +
                    Number.parseFloat(String(row.valor_mensalidade ?? "0")),
                  0,
                )
                .toFixed(2),
            },
            ...describeReportScope(scope, referenceDate),
            columns: selectedColumns,
          },
        });
      } catch (error) {
        toast.error(
          error instanceof Error
            ? error.message
            : "Falha ao exportar contratos da tesouraria.",
        );
      } finally {
        setIsExporting(false);
      }
    },
    [
      agenteFiltro,
      dataFim,
      dataInicio,
      ordering,
      search,
      situacaoEsteira,
      statusContrato,
      visibleSection,
    ],
  );

  const handleRefresh = () => {
    setPagePending(1);
    setPageReactivation(1);
    setPagePaid(1);
    setPageLiquidated(1);
    setPageCanceled(1);
    void queryClient.invalidateQueries({ queryKey: ["tesouraria-contratos"] });
  };

  const buildRangeLabel = (page: number, rowCount: number, total: number) => {
    if (!rowCount) return "0";
    const start = (page - 1) * PAGE_SIZE + 1;
    return `${start}-${start + rowCount - 1} de ${total}`;
  };

  const pendingRows = pendingQuery.data?.results ?? [];
  const reactivationRows = reactivationQuery.data?.results ?? [];
  const paidRows = paidQuery.data?.results ?? [];
  const liquidatedRows = liquidatedQuery.data?.results ?? [];
  const canceledRows = canceledQuery.data?.results ?? [];
  const activeAdvancedFiltersCount = countAdvancedFilters({
    dataInicio,
    dataFim,
    agente: agenteFiltro,
    statusContrato,
    situacaoEsteira,
    ordering,
  });
  const hasActiveFilters = activeAdvancedFiltersCount > 0 || Boolean(search);
  const filterScopeLabel = formatActiveFilterScope({
    dataInicio,
    dataFim,
    hasActiveFilters,
  });
  const kpiCounts = {
    pendente: pendingQuery.data?.count ?? 0,
    reativacao: reactivationQuery.data?.count ?? 0,
    concluido: paidQuery.data?.count ?? 0,
    liquidado: liquidatedQuery.data?.count ?? 0,
    cancelado: canceledQuery.data?.count ?? 0,
  };
  const totalKpiCount =
    kpiCounts.pendente +
    kpiCounts.reativacao +
    kpiCounts.concluido +
    kpiCounts.liquidado +
    kpiCounts.cancelado;
  const shouldShowSection = (section: TesourariaSectionFilter) =>
    visibleSection === "todos" || visibleSection === section;
  const kpiCards = [
    {
      key: "todos" as const,
      label: "Total no filtro",
      tooltip: "Soma das filas operacionais exibidas com o recorte atual.",
      value: totalKpiCount,
      tone: "neutral" as const,
      icon: LayoutGridIcon,
    },
    {
      key: "pendente" as const,
      label: "Pendentes",
      tooltip: "Contratos ainda sem liberação final na tesouraria.",
      value: kpiCounts.pendente,
      tone: "warning" as const,
      icon: Clock3Icon,
    },
    {
      key: "reativacao" as const,
      label: "Reativações",
      tooltip: "Contratos de reativação aguardando efetivação na tesouraria.",
      value: kpiCounts.reativacao,
      tone: "positive" as const,
      icon: CheckCircle2Icon,
    },
    {
      key: "concluido" as const,
      label: "Efetivados",
      tooltip: "Contratos já liberados pela tesouraria no recorte atual.",
      value: kpiCounts.concluido,
      tone: "positive" as const,
      icon: CheckCircle2Icon,
    },
    {
      key: "liquidado" as const,
      label: "Liquidados",
      tooltip: "Contratos encerrados operacionalmente.",
      value: kpiCounts.liquidado,
      tone: "neutral" as const,
      icon: HandCoinsIcon,
    },
    {
      key: "cancelado" as const,
      label: "Cancelados",
      tooltip: "Contratos cancelados ou desistentes no histórico do recorte.",
      value: kpiCounts.cancelado,
      tone: "warning" as const,
      icon: XCircleIcon,
    },
  ];
  const agentOptions = React.useMemo(
    () =>
      (agentesFiltroQuery.data ?? []).map((item) => ({
        value: String(item.id),
        label: [
          item.full_name || item.email || `Usuário ${item.id}`,
          item.primary_role || null,
          item.email || null,
        ]
          .filter(Boolean)
          .join(" · "),
      })),
    [agentesFiltroQuery.data],
  );

  return (
    <div className="space-y-6">
      <section className="space-y-4">
        <Card className="rounded-[1.75rem] border-border/60 bg-card/70">
          <CardContent className="flex flex-col gap-4 p-6">
            <div>
              <p className="text-sm uppercase tracking-[0.28em] text-muted-foreground">
                Tesouraria
              </p>
              <h1 className="text-3xl font-semibold">Novos Contratos</h1>
              <p className="text-sm text-muted-foreground">
                Painel operacional de novos contratos, pagamentos iniciais e
                histórico da tesouraria.
              </p>
            </div>
          </CardContent>
        </Card>
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-5">
          {kpiCards.map((card) => (
            <StatsCard
              key={card.key}
              title={card.label}
              tooltip={card.tooltip}
              value={String(card.value)}
              delta={filterScopeLabel}
              tone={card.tone}
              icon={card.icon}
              active={visibleSection === card.key}
              onClick={() => setVisibleSection(card.key)}
            />
          ))}
        </div>
      </section>

      <section className="grid gap-3 rounded-[1.75rem] border border-border/60 bg-card/50 p-4 lg:grid-cols-[minmax(0,1fr)_auto_auto]">
        <Input
          value={search}
          onChange={(event) => setSearch(event.target.value)}
          placeholder="Buscar por nome, CPF/CNPJ, matrícula ou código do contrato"
          className="rounded-2xl border-border/60 bg-card/60"
        />
        <Sheet
          onOpenChange={(open) => {
            if (open) {
              setDraftAdvancedFilters({
                dataInicio,
                dataFim,
                agente: agenteFiltro,
                statusContrato,
                situacaoEsteira,
                ordering,
              });
            }
          }}
        >
          <SheetTrigger asChild>
            <Button variant="outline" className="rounded-2xl">
              <SlidersHorizontalIcon className="size-4" />
              Filtros avançados
              {activeAdvancedFiltersCount ? (
                <Badge className="ml-1 rounded-full bg-primary/15 px-2 py-0 text-primary">
                  {activeAdvancedFiltersCount}
                </Badge>
              ) : null}
            </Button>
          </SheetTrigger>
          <SheetContent className="w-full border-l border-border/60 sm:max-w-xl">
            <SheetHeader>
              <SheetTitle>Filtros avançados</SheetTitle>
              <SheetDescription>
                Filtre por data de solicitação, agente, status do contrato e
                situação operacional.
              </SheetDescription>
            </SheetHeader>

            <div className="space-y-5 overflow-y-auto px-4 pb-4">
              <FilterField label="Agente">
                <SearchableSelect
                  options={agentOptions}
                  value={draftAdvancedFilters.agente}
                  onChange={(value) =>
                    setDraftAdvancedFilters((current) => ({
                      ...current,
                      agente: value,
                    }))
                  }
                  placeholder="Todos os responsáveis"
                  searchPlaceholder="Buscar usuário"
                  clearLabel="Todos os responsáveis"
                />
              </FilterField>

              <div className="grid gap-4 md:grid-cols-3">
                <FilterField label="Ordem">
                  <Select
                    value={draftAdvancedFilters.ordering}
                    onValueChange={(value) =>
                      setDraftAdvancedFilters((current) => ({
                        ...current,
                        ordering: value,
                      }))
                    }
                  >
                    <SelectTrigger className="rounded-2xl bg-card/60">
                      <SelectValue placeholder="Ordem decrescente" />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="-created_at">
                        Ordem decrescente
                      </SelectItem>
                      <SelectItem value="created_at">
                        Ordem crescente
                      </SelectItem>
                    </SelectContent>
                  </Select>
                </FilterField>
                <FilterField label="Solicitação a partir de">
                  <DatePicker
                    value={draftAdvancedFilters.dataInicio}
                    onChange={(value) =>
                      setDraftAdvancedFilters((current) => ({
                        ...current,
                        dataInicio: value,
                      }))
                    }
                  />
                </FilterField>

                <FilterField label="Solicitação até">
                  <DatePicker
                    value={draftAdvancedFilters.dataFim}
                    onChange={(value) =>
                      setDraftAdvancedFilters((current) => ({
                        ...current,
                        dataFim: value,
                      }))
                    }
                  />
                </FilterField>
              </div>

              <div className="grid gap-4 md:grid-cols-2">
                <FilterField label="Status do contrato">
                  <Select
                    value={draftAdvancedFilters.statusContrato}
                    onValueChange={(value) =>
                      setDraftAdvancedFilters((current) => ({
                        ...current,
                        statusContrato: value,
                      }))
                    }
                  >
                    <SelectTrigger className="rounded-2xl bg-card/60">
                      <SelectValue placeholder="Todos contratos" />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="todos">Todos contratos</SelectItem>
                      <SelectItem value="em_analise">Em análise</SelectItem>
                      <SelectItem value="congelado">Congelados</SelectItem>
                      <SelectItem value="ativo">Ativos</SelectItem>
                      <SelectItem value="encerrado">Encerrados</SelectItem>
                      <SelectItem value="cancelado">Cancelados</SelectItem>
                    </SelectContent>
                  </Select>
                </FilterField>

                <FilterField label="Situação operacional">
                  <Select
                    value={draftAdvancedFilters.situacaoEsteira}
                    onValueChange={(value) =>
                      setDraftAdvancedFilters((current) => ({
                        ...current,
                        situacaoEsteira: value,
                      }))
                    }
                  >
                    <SelectTrigger className="rounded-2xl bg-card/60">
                      <SelectValue placeholder="Todas situações" />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="todos">Todas situações</SelectItem>
                      <SelectItem value="aguardando">Aguardando</SelectItem>
                      <SelectItem value="em_andamento">Em andamento</SelectItem>
                      <SelectItem value="aprovado">Aprovados</SelectItem>
                      <SelectItem value="pendenciado">Pendenciados</SelectItem>
                      <SelectItem value="rejeitado">Rejeitados</SelectItem>
                    </SelectContent>
                  </Select>
                </FilterField>
              </div>
            </div>

            <SheetFooter className="border-t border-border/60">
              <Button
                variant="outline"
                type="button"
                onClick={() => {
                  setAgenteFiltro("");
                  setDataInicio(undefined);
                  setDataFim(undefined);
                  setStatusContrato("todos");
                  setSituacaoEsteira("todos");
                  setOrdering("-created_at");
                  setDraftAdvancedFilters({
                    dataInicio: undefined,
                    dataFim: undefined,
                    agente: "",
                    statusContrato: "todos",
                    situacaoEsteira: "todos",
                    ordering: "-created_at",
                  });
                  handleRefresh();
                }}
              >
                Limpar avançados
              </Button>
              <SheetClose asChild>
                <Button
                  type="button"
                  onClick={() => {
                    setAgenteFiltro(draftAdvancedFilters.agente);
                    setDataInicio(draftAdvancedFilters.dataInicio);
                    setDataFim(draftAdvancedFilters.dataFim);
                    setStatusContrato(draftAdvancedFilters.statusContrato);
                    setSituacaoEsteira(draftAdvancedFilters.situacaoEsteira);
                    setOrdering(draftAdvancedFilters.ordering);
                    handleRefresh();
                  }}
                >
                  Aplicar
                </Button>
              </SheetClose>
            </SheetFooter>
          </SheetContent>
        </Sheet>
        <ReportExportDialog
          disabled={isExporting}
          label="Exportar"
          showFilters
          agentOptions={agentOptions}
          originOptions={[
            { value: "cadastro", label: "Novos contratos" },
            { value: "reativacao", label: "Reativações" },
          ]}
          statusOptions={[
            { value: "ativo", label: "Ativo" },
            { value: "cancelado", label: "Cancelado" },
            { value: "encerrado", label: "Encerrado" },
          ]}
          esteiraOptions={[
            { value: "cadastro", label: "Cadastro" },
            { value: "analise", label: "Análise" },
            { value: "coordenacao", label: "Coordenação" },
            { value: "tesouraria", label: "Tesouraria" },
            { value: "concluido", label: "Concluído" },
          ]}
          initialScope={
            dataInicio &&
            dataFim &&
            toFilterDate(dataInicio) === toFilterDate(dataFim)
              ? "day"
              : "month"
          }
          initialDayRef={dataFim ?? dataInicio}
          initialMonthRef={dataFim ?? dataInicio}
          onExport={handleExport}
        />
      </section>

      {shouldShowSection("pendente") ? (
        <section className="space-y-4">
          <header className="flex items-center justify-between">
            <div>
              <p className="text-xs uppercase tracking-[0.28em] text-muted-foreground">
                Pendentes
              </p>
              <h2 className="text-xl font-semibold">
                Aguardando efetivação PIX
              </h2>
            </div>
            <p className="text-sm text-muted-foreground">
              Mostrando{" "}
              {buildRangeLabel(
                pagePending,
                pendingRows.length,
                pendingQuery.data?.count ?? 0,
              )}
            </p>
          </header>
          <DataTable
            data={pendingRows}
            columns={columns}
            currentPage={pagePending}
            totalPages={Math.max(
              1,
              Math.ceil((pendingQuery.data?.count ?? 0) / PAGE_SIZE),
            )}
            onPageChange={setPagePending}
            emptyMessage="Nenhum contrato pendente para os filtros informados."
            loading={pendingQuery.isLoading}
            skeletonRows={PAGE_SIZE}
          />
        </section>
      ) : null}

      {shouldShowSection("reativacao") ? (
        <section className="space-y-4">
          <header className="flex items-center justify-between">
            <div>
              <p className="text-xs uppercase tracking-[0.28em] text-emerald-300">
                Reativações
              </p>
              <h2 className="text-xl font-semibold">
                Reativações aguardando tesouraria
              </h2>
            </div>
            <p className="text-sm text-muted-foreground">
              Mostrando{" "}
              {buildRangeLabel(
                pageReactivation,
                reactivationRows.length,
                reactivationQuery.data?.count ?? 0,
              )}
            </p>
          </header>
          <DataTable
            data={reactivationRows}
            columns={columns}
            currentPage={pageReactivation}
            totalPages={Math.max(
              1,
              Math.ceil((reactivationQuery.data?.count ?? 0) / PAGE_SIZE),
            )}
            onPageChange={setPageReactivation}
            emptyMessage="Nenhuma reativação pendente para os filtros informados."
            loading={reactivationQuery.isLoading}
            skeletonRows={PAGE_SIZE}
          />
        </section>
      ) : null}

      {shouldShowSection("concluido") ? (
        <section className="space-y-4">
          <header className="flex items-center justify-between">
            <div>
              <p className="text-xs uppercase tracking-[0.28em] text-emerald-300">
                Pagos
              </p>
              <h2 className="text-xl font-semibold">Contratos efetivados</h2>
            </div>
            <p className="text-sm text-muted-foreground">
              Mostrando{" "}
              {buildRangeLabel(
                pagePaid,
                paidRows.length,
                paidQuery.data?.count ?? 0,
              )}
            </p>
          </header>
          <DataTable
            data={paidRows}
            columns={columns}
            currentPage={pagePaid}
            totalPages={Math.max(
              1,
              Math.ceil((paidQuery.data?.count ?? 0) / PAGE_SIZE),
            )}
            onPageChange={setPagePaid}
            emptyMessage="Nenhum contrato pago para os filtros informados."
            loading={paidQuery.isLoading}
            skeletonRows={PAGE_SIZE}
          />
        </section>
      ) : null}

      {shouldShowSection("liquidado") ? (
        <section className="space-y-4">
          <header className="flex items-center justify-between">
            <div>
              <p className="text-xs uppercase tracking-[0.28em] text-emerald-300">
                Liquidados
              </p>
              <h2 className="text-xl font-semibold">Contratos liquidados</h2>
            </div>
            <p className="text-sm text-muted-foreground">
              Mostrando{" "}
              {buildRangeLabel(
                pageLiquidated,
                liquidatedRows.length,
                liquidatedQuery.data?.count ?? 0,
              )}
            </p>
          </header>
          <DataTable
            data={liquidatedRows}
            columns={columns}
            currentPage={pageLiquidated}
            totalPages={Math.max(
              1,
              Math.ceil((liquidatedQuery.data?.count ?? 0) / PAGE_SIZE),
            )}
            onPageChange={setPageLiquidated}
            emptyMessage="Nenhum contrato liquidado para os filtros informados."
            loading={liquidatedQuery.isLoading}
            skeletonRows={PAGE_SIZE}
          />
        </section>
      ) : null}

      {shouldShowSection("cancelado") ? (
        <section className="space-y-4">
          <header className="flex items-center justify-between">
            <div>
              <p className="text-xs uppercase tracking-[0.28em] text-rose-300">
                Cancelados / Desistentes
              </p>
              <h2 className="text-xl font-semibold">
                Contratos cancelados / desistentes
              </h2>
            </div>
            <p className="text-sm text-muted-foreground">
              Mostrando{" "}
              {buildRangeLabel(
                pageCanceled,
                canceledRows.length,
                canceledQuery.data?.count ?? 0,
              )}
            </p>
          </header>
          <DataTable
            data={canceledRows}
            columns={columns}
            currentPage={pageCanceled}
            totalPages={Math.max(
              1,
              Math.ceil((canceledQuery.data?.count ?? 0) / PAGE_SIZE),
            )}
            onPageChange={setPageCanceled}
            emptyMessage="Nenhum contrato cancelado ou desistente para os filtros informados."
            loading={canceledQuery.isLoading}
            skeletonRows={PAGE_SIZE}
          />
        </section>
      ) : null}

      <Dialog
        open={!!freezeTarget}
        onOpenChange={(open) => !open && setFreezeTarget(null)}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Congelar contrato</DialogTitle>
            <DialogDescription>
              Registre o motivo para pausar temporariamente a efetivação.
            </DialogDescription>
          </DialogHeader>
          <Textarea
            value={freezeReason}
            onChange={(event) => setFreezeReason(event.target.value)}
            placeholder="Descreva o motivo do congelamento..."
            className="min-h-32"
          />
          <DialogFooter>
            <Button variant="outline" onClick={() => setFreezeTarget(null)}>
              Cancelar
            </Button>
            <Button
              onClick={() => {
                if (!freezeTarget || !freezeReason.trim()) {
                  toast.error("Informe um motivo para congelar o contrato.");
                  return;
                }
                congelarMutation.mutate({
                  contratoId: freezeTarget.id,
                  motivo: freezeReason,
                });
              }}
              disabled={congelarMutation.isPending}
            >
              Confirmar congelamento
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog
        open={!!pendenciarTarget}
        onOpenChange={(open) => {
          if (!open) {
            setPendenciarTarget(null);
            setPendenciaDescricao("");
          }
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Pendenciar para análise</DialogTitle>
            <DialogDescription>
              Devolva o contrato para a análise com a orientação do que precisa
              ser revisado antes da efetivação.
            </DialogDescription>
          </DialogHeader>
          <Textarea
            value={pendenciaDescricao}
            onChange={(event) => setPendenciaDescricao(event.target.value)}
            placeholder="Descreva o ajuste necessário para a análise..."
            className="min-h-32"
          />
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => {
                setPendenciarTarget(null);
                setPendenciaDescricao("");
              }}
            >
              Cancelar
            </Button>
            <Button
              onClick={() => {
                if (!pendenciarTarget || !pendenciaDescricao.trim()) {
                  toast.error(
                    "Informe o motivo para devolver o contrato à análise.",
                  );
                  return;
                }
                pendenciarMutation.mutate({
                  contratoId: pendenciarTarget.id,
                  descricao: pendenciaDescricao,
                });
              }}
              disabled={pendenciarMutation.isPending}
            >
              Enviar para análise
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog
        open={!!bankTarget}
        onOpenChange={(open) => !open && setBankTarget(null)}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Dados bancários</DialogTitle>
            <DialogDescription>
              Use estas informações para executar a transferência PIX do
              contrato.
            </DialogDescription>
          </DialogHeader>
          <div className="grid gap-3 rounded-2xl border border-border/60 bg-card/60 p-4 text-sm">
            <InfoLine
              label="Banco"
              value={bankTarget?.dados_bancarios?.banco}
            />
            <InfoLine
              label="Agência"
              value={bankTarget?.dados_bancarios?.agencia}
            />
            <InfoLine
              label="Conta"
              value={bankTarget?.dados_bancarios?.conta}
            />
            <InfoLine
              label="Tipo"
              value={bankTarget?.dados_bancarios?.tipo_conta}
            />
            <InfoLine
              label="Chave PIX"
              value={bankTarget?.dados_bancarios?.chave_pix || "—"}
            />
          </div>
        </DialogContent>
      </Dialog>

      <Dialog
        open={!!cancelTarget}
        onOpenChange={(open) => {
          if (!open) {
            setCancelTarget(null);
            setCancelReason("");
            setCancelType("cancelado");
          }
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Cancelar contrato</DialogTitle>
            <DialogDescription>
              Escolha se o contrato foi cancelado internamente ou se o cliente
              desistiu antes da ativação.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4">
            <Select
              value={cancelType}
              onValueChange={(value) =>
                setCancelType(value as "cancelado" | "desistente")
              }
            >
              <SelectTrigger className="rounded-2xl bg-card/60">
                <SelectValue placeholder="Tipo do cancelamento" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="cancelado">Cancelado</SelectItem>
                <SelectItem value="desistente">Desistente</SelectItem>
              </SelectContent>
            </Select>
            <Textarea
              value={cancelReason}
              onChange={(event) => setCancelReason(event.target.value)}
              placeholder="Motivo do cancelamento"
              className="min-h-32"
            />
          </div>
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => {
                setCancelTarget(null);
                setCancelReason("");
                setCancelType("cancelado");
              }}
            >
              Voltar
            </Button>
            <Button
              variant="destructive"
              disabled={
                !cancelTarget ||
                !cancelReason.trim() ||
                cancelarMutation.isPending
              }
              onClick={() => {
                if (!cancelTarget || !cancelReason.trim()) {
                  toast.error("Informe o motivo do cancelamento.");
                  return;
                }
                cancelarMutation.mutate({
                  contratoId: cancelTarget.id,
                  tipo: cancelType,
                  motivo: cancelReason,
                });
              }}
            >
              Confirmar cancelamento
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog
        open={!!effectivationTarget}
        onOpenChange={(open) => !open && setEffectivationTarget(null)}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Confirmar ciclo da reativação</DialogTitle>
            <DialogDescription>
              A efetivação fechará o ciclo anterior e abrirá um novo ciclo com
              as parcelas confirmadas abaixo.
            </DialogDescription>
          </DialogHeader>
          {effectivationTarget ? (
            <div className="space-y-4">
              <div className="rounded-2xl border border-border/60 bg-card/60 p-4 text-sm">
                <p className="font-medium">{effectivationTarget.nome}</p>
                <p className="text-muted-foreground">
                  {effectivationTarget.matricula || "Sem matrícula"} ·{" "}
                  {maskCPFCNPJ(effectivationTarget.cpf_cnpj)}
                </p>
              </div>
              <div className="grid gap-3 rounded-2xl border border-emerald-500/30 bg-emerald-500/10 p-4 text-sm">
                <InfoLine
                  label="Última parcela paga"
                  value={
                    effectivationTarget.reactivation_cycle_preview
                      ?.ultima_parcela_paga
                      ? formatCompetenciaLabel(
                          effectivationTarget.reactivation_cycle_preview
                            .ultima_parcela_paga,
                        )
                      : "Sem parcela liquidada anterior"
                  }
                />
                <InfoLine
                  label="Início sugerido"
                  value={
                    effectivationTarget.reactivation_cycle_preview
                      ?.competencia_inicial_sugerida
                      ? formatCompetenciaLabel(
                          effectivationTarget.reactivation_cycle_preview
                            .competencia_inicial_sugerida,
                        )
                      : "Não calculado"
                  }
                />
                <div className="space-y-2">
                  <p className="text-xs font-medium uppercase tracking-[0.18em] text-muted-foreground">
                    Parcelas do novo ciclo
                  </p>
                  <div className="flex flex-wrap gap-2">
                    {effectivationTarget.reactivation_cycle_preview
                      ?.competencias_sugeridas?.length ? (
                      effectivationTarget.reactivation_cycle_preview.competencias_sugeridas.map(
                        (competencia) => (
                          <Badge
                            key={competencia}
                            className="rounded-full bg-emerald-500/15 text-emerald-200"
                          >
                            {formatCompetenciaLabel(competencia)}
                          </Badge>
                        ),
                      )
                    ) : (
                      <span className="text-muted-foreground">
                        Nenhuma parcela sugerida pela API.
                      </span>
                    )}
                  </div>
                </div>
              </div>
            </div>
          ) : null}
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => setEffectivationTarget(null)}
            >
              Voltar
            </Button>
            <Button
              disabled={
                !effectivationTarget ||
                !effectivationTarget.reactivation_cycle_preview
                  ?.competencias_sugeridas?.length ||
                efetivarMutation.isPending
              }
              onClick={() => {
                const competencias =
                  effectivationTarget?.reactivation_cycle_preview
                    ?.competencias_sugeridas;
                if (!effectivationTarget || !competencias?.length) {
                  toast.error(
                    "A API não retornou parcelas sugeridas para a reativação.",
                  );
                  return;
                }
                efetivarMutation.mutate({
                  contratoId: effectivationTarget.id,
                  competenciasCiclo: competencias,
                });
              }}
            >
              Confirmar e efetivar
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog
        open={!!deleteTarget}
        onOpenChange={(open) => !open && setDeleteTarget(null)}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              {deleteTarget
                ? getOperationalDeleteCopy(deleteTarget).title
                : "Remover da fila"}
            </DialogTitle>
            <DialogDescription>
              {deleteTarget
                ? getOperationalDeleteCopy(deleteTarget).description
                : "Confirme a exclusão do item operacional."}
            </DialogDescription>
          </DialogHeader>
          {deleteTarget ? (
            <div className="rounded-2xl border border-border/60 bg-card/60 p-4 text-sm">
              <p className="font-medium">{deleteTarget.nome}</p>
              <p className="text-muted-foreground">
                {deleteTarget.matricula || "Sem matrícula"} ·{" "}
                {maskCPFCNPJ(deleteTarget.cpf_cnpj)}
              </p>
            </div>
          ) : null}
          <DialogFooter>
            <Button variant="outline" onClick={() => setDeleteTarget(null)}>
              Voltar
            </Button>
            <Button
              variant="destructive"
              disabled={!deleteTarget || excluirMutation.isPending}
              onClick={() => {
                if (!deleteTarget) {
                  return;
                }
                excluirMutation.mutate({ contratoId: deleteTarget.id });
              }}
            >
              {deleteTarget
                ? getOperationalDeleteCopy(deleteTarget).confirmLabel
                : "Confirmar"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <AssociadoDetailsDialog
        open={!!detailTarget}
        onOpenChange={(open) => !open && setDetailTarget(null)}
        associadoId={detailTarget?.associado_id ?? null}
        description="Consulta expandida do associado, contratos e ciclos diretamente na fila de novos contratos."
      />
    </div>
  );
}

function FilterField({
  label,
  children,
}: React.PropsWithChildren<{ label: string }>) {
  return (
    <div className="space-y-2">
      <p className="text-[11px] font-medium uppercase tracking-[0.18em] text-muted-foreground">
        {label}
      </p>
      {children}
    </div>
  );
}

function ComprovanteSlot({
  label,
  existingUrl,
  existingName,
  draftFile,
  disabled,
  isProcessing,
  compact,
  onSelect,
  onClear,
}: {
  label: string;
  existingUrl?: string;
  existingName?: string;
  draftFile?: File;
  disabled?: boolean;
  isProcessing?: boolean;
  compact?: boolean;
  onSelect: (file: File) => void;
  onClear: () => void;
}) {
  const inputRef = React.useRef<HTMLInputElement | null>(null);
  const accept = React.useMemo(
    () => Object.values(comprovanteAccept).flat().join(","),
    [],
  );
  const canReplace = !disabled && !isProcessing;
  const handleFileSelection = (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    if (!file) {
      return;
    }
    onSelect(file);
    event.target.value = "";
  };

  const openPicker = () => {
    if (!canReplace) {
      return;
    }
    inputRef.current?.click();
  };

  const displayName = draftFile?.name || existingName;
  const displayUrl = draftFile ? undefined : existingUrl;
  const primaryActionLabel = label;
  const openExistingFile = React.useCallback(
    (event?: React.MouseEvent<HTMLButtonElement>) => {
      if (!displayUrl) {
        return;
      }
      event?.preventDefault();
      event?.stopPropagation();
      window.open(
        buildBackendFileUrl(displayUrl),
        "_blank",
        "noopener,noreferrer",
      );
    },
    [displayUrl],
  );

  return (
    <div
      className={
        compact
          ? "rounded-xl border border-border/60 bg-background/40 p-2"
          : "rounded-2xl border border-border/60 bg-background/40 p-3"
      }
    >
      <input
        ref={inputRef}
        type="file"
        className="hidden"
        accept={accept}
        disabled={!canReplace}
        onChange={handleFileSelection}
      />
      {compact ? (
        <div className="space-y-2">
          {displayUrl ? (
            <Button
              type="button"
              size="xs"
              variant="outline"
              className="w-full justify-start"
              onClick={openExistingFile}
              title={displayName || `Ver comprovante de ${label.toLowerCase()}`}
            >
              <PaperclipIcon className="size-4" />
              {`Ver ${primaryActionLabel.toLowerCase()}`}
            </Button>
          ) : (
            <Button
              size="xs"
              variant="outline"
              className="w-full justify-start"
              onClick={openPicker}
              disabled={!canReplace}
            >
              <UploadIcon className="size-4" />
              {primaryActionLabel}
            </Button>
          )}
          <div className="flex gap-2">
            <Button
              size="xs"
              variant="outline"
              className="flex-1"
              onClick={openPicker}
              disabled={!canReplace}
            >
              <UploadIcon className="size-4" />
              {displayName ? "Adicionar versão" : "Enviar"}
            </Button>
            {draftFile ? (
              <Button
                size="icon-sm"
                variant="ghost"
                onClick={onClear}
                type="button"
              >
                <Trash2Icon className="size-4" />
              </Button>
            ) : null}
          </div>
        </div>
      ) : (
        <div className="space-y-2">
          <div className="mb-2 flex items-center justify-between gap-3">
            <p className="text-[11px] font-semibold uppercase tracking-[0.24em] text-muted-foreground">
              {label}
            </p>
            {draftFile ? (
              <Button size="icon-sm" variant="ghost" onClick={onClear}>
                <Trash2Icon className="size-4" />
              </Button>
            ) : null}
          </div>
          {displayName ? (
            <div className="space-y-1 text-sm">
              <p className="font-medium" title={displayName}>
                {displayName}
              </p>
              {draftFile ? (
                <p className="text-xs text-muted-foreground">
                  {(draftFile.size / 1024 / 1024).toFixed(2)} MB pronto para
                  envio.
                </p>
              ) : (
                <p className="text-xs text-muted-foreground">
                  Arquivo já anexado.
                </p>
              )}
            </div>
          ) : (
            <p className="text-xs text-muted-foreground">
              Nenhum arquivo enviado.
            </p>
          )}

          <div className="flex flex-wrap gap-2">
            {displayUrl ? (
              <Button
                type="button"
                size="sm"
                variant="outline"
                onClick={openExistingFile}
              >
                <PaperclipIcon className="size-4" />
                Ver arquivo
              </Button>
            ) : null}
            <Button
              size="sm"
              variant="outline"
              onClick={openPicker}
              disabled={!canReplace}
            >
              <UploadIcon className="size-4" />
              {draftFile ? "Trocar" : displayUrl ? "Adicionar versão" : "Enviar"}
            </Button>
          </div>
          <p className="text-[11px] text-muted-foreground">
            PDF, JPG ou PNG até 5 MB.
          </p>
        </div>
      )}
    </div>
  );
}

function InfoLine({ label, value }: { label: string; value?: string }) {
  return (
    <div className="flex items-center justify-between gap-4">
      <span className="text-muted-foreground">{label}</span>
      <span className="font-medium capitalize">{value || "—"}</span>
    </div>
  );
}
