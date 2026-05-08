"use client";

import * as React from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Controller, type Resolver, useForm } from "react-hook-form";
import { z } from "zod";
import {
  ArrowLeftIcon,
  ArrowRightIcon,
  CheckIcon,
  SaveIcon,
} from "lucide-react";
import { toast } from "sonner";

import type { AssociadoDetail, Documento, SimpleUser } from "@/lib/api/types";
import { apiFetch } from "@/lib/api/client";
import { buildBackendFileUrl } from "@/lib/backend-files";
import {
  centsToDecimal,
  decimalToCents,
  formatCurrency,
  formatLongMonthYear,
} from "@/lib/formatters";
import { onlyDigits } from "@/lib/masks";
import { useAuth } from "@/hooks/use-auth";
import { usePermissions } from "@/hooks/use-permissions";
import { useRouteTransition } from "@/providers/route-transition-provider";
import DatePicker from "@/components/custom/date-picker";
import FileUploadDropzone from "@/components/custom/file-upload-dropzone";
import InputCep from "@/components/custom/input-cep";
import InputCpfCnpj from "@/components/custom/input-cpf-cnpj";
import InputCurrency from "@/components/custom/input-currency";
import InputPixKey from "@/components/custom/input-pix-key";
import InputPhone from "@/components/custom/input-phone";
import StatusBadge from "@/components/custom/status-badge";
import {
  addMonths,
  calculateContratoDates,
  parseIsoDate,
  startOfLocalDay,
} from "@/components/associados/contrato-dates";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Field,
  FieldContent,
  FieldError,
  FieldGroup,
  FieldLabel,
} from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Checkbox } from "@/components/ui/checkbox";
import { Textarea } from "@/components/ui/textarea";

const ufOptions = [
  "AC",
  "AL",
  "AP",
  "AM",
  "BA",
  "CE",
  "DF",
  "ES",
  "GO",
  "MA",
  "MT",
  "MS",
  "MG",
  "PA",
  "PB",
  "PR",
  "PE",
  "PI",
  "RJ",
  "RN",
  "RS",
  "RO",
  "RR",
  "SC",
  "SP",
  "SE",
  "TO",
];

const situacaoServidorOptions = [
  { value: "ativo", label: "Ativo" },
  { value: "afastado", label: "Afastado" },
  { value: "aposentado", label: "Aposentado" },
  { value: "pensionista", label: "Pensionista" },
  { value: "comissionado", label: "Comissionado" },
  { value: "contratado", label: "Contratado" },
];

const estadoCivilOptions = [
  { value: "solteiro", label: "Solteiro" },
  { value: "casado", label: "Casado" },
  { value: "divorciado", label: "Divorciado" },
  { value: "viuvo", label: "Viúvo" },
  { value: "uniao_estavel", label: "União estável" },
];

const associadoStatusOptions = [
  { value: "cadastrado", label: "Cadastrado" },
  { value: "importado", label: "Importado" },
  { value: "em_analise", label: "Em análise" },
  { value: "ativo", label: "Ativo" },
  { value: "apto_a_renovar", label: "Apto a renovar" },
  { value: "pendente", label: "Pendente" },
  { value: "inativo", label: "Inativo" },
  { value: "inadimplente", label: "Inadimplente" },
] as const;

const contratoStatusOptions = [
  { value: "rascunho", label: "Rascunho" },
  { value: "em_analise", label: "Em análise" },
  { value: "ativo", label: "Ativo" },
  { value: "encerrado", label: "Encerrado" },
  { value: "cancelado", label: "Cancelado" },
] as const;

const documentFields = [
  { key: "documento_frente", label: "Documento (frente)" },
  { key: "documento_verso", label: "Documento (verso)" },
  { key: "comprovante_residencia", label: "Comprovante de residência" },
  { key: "contracheque", label: "Contracheque atual" },
  { key: "termo_adesao", label: "Termo de adesão" },
  { key: "termo_antecipacao", label: "Termo de antecipação" },
  { key: "anexo_extra_1", label: "Anexo extra 1" },
  { key: "anexo_extra_2", label: "Anexo extra 2" },
] as const;

const PRAZO_ANTECIPACAO_PADRAO = 3;
const PRAZO_ANTECIPACAO_OPTIONS = [
  { value: "3", label: "3 parcelas" },
  { value: "4", label: "4 parcelas" },
];
const TAXA_ANTECIPACAO_PADRAO = 30;

function getDocumentoOrder(documento: Documento) {
  const timestamp =
    Date.parse(documento.updated_at ?? "") ||
    Date.parse(documento.created_at ?? "") ||
    0;
  return timestamp || documento.id;
}

const schema = z
  .object({
    tipo_documento: z.enum(["CPF", "CNPJ"]),
    cpf_cnpj: z.string(),
    rg: z.string().optional(),
    orgao_expedidor: z.string().optional(),
    nome_completo: z.string().min(3, "Nome completo é obrigatório."),
    data_nascimento: z
      .date()
      .optional()
      .refine(
        (value) => !value || value < new Date(),
        "A data deve estar no passado.",
      ),
    profissao: z.string().optional(),
    estado_civil: z.string().optional(),
    status: z.string().optional(),
    status_contrato: z.string().optional(),
    endereco: z.object({
      cep: z
        .string()
        .refine((value) => onlyDigits(value).length === 8, "CEP inválido."),
      endereco: z.string().min(3, "Endereço é obrigatório."),
      numero: z.string(),
      complemento: z.string().optional(),
      bairro: z.string().min(2, "Bairro é obrigatório."),
      cidade: z.string().min(2, "Cidade é obrigatória."),
      uf: z.string().min(2, "UF é obrigatória."),
    }),
    dados_bancarios: z.object({
      banco: z.string().min(1, "Banco é obrigatório."),
      agencia: z.string().min(1, "Agência é obrigatória."),
      conta: z.string().min(1, "Conta é obrigatória."),
      tipo_conta: z.string().min(1, "Tipo de conta é obrigatório."),
      chave_pix: z.string().optional(),
    }),
    contato: z.object({
      celular: z.string().min(14, "Celular é obrigatório."),
      email: z.email("E-mail inválido."),
      orgao_publico: z.string().min(1, "Órgão público é obrigatório."),
      situacao_servidor: z
        .string()
        .min(1, "Situação do servidor é obrigatória."),
      matricula_servidor: z
        .string()
        .min(1, "Matrícula do servidor é obrigatória."),
    }),
    contrato: z.object({
      valor_bruto_total: z
        .number()
        .nullable()
        .refine((value) => (value ?? 0) > 0, "Informe o valor bruto."),
      valor_liquido: z
        .number()
        .nullable()
        .refine((value) => (value ?? 0) > 0, "Informe o valor líquido."),
      prazo_meses: z
        .number()
        .min(3, "Prazo inválido.")
        .max(4, "Prazo inválido."),
      taxa_antecipacao: z.number().min(0, "Taxa inválida."),
      mensalidade: z
        .number()
        .nullable()
        .refine(
          (value) => (value ?? 0) > 0,
          "A mensalidade deve ser maior que zero.",
        ),
      data_aprovacao: z.date().optional(),
    }),
    agente_responsavel_id: z.number().nullable().optional(),
    percentual_repasse: z.number().nullable().optional(),
  })
  .superRefine((values, context) => {
    const digits = onlyDigits(values.cpf_cnpj);
    if (!digits) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["cpf_cnpj"],
        message: "CPF/CNPJ é obrigatório.",
      });
      return;
    }

    if (values.tipo_documento === "CPF" && digits.length !== 11) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["cpf_cnpj"],
        message: "CPF deve ter 11 dígitos.",
      });
    }

    if (values.tipo_documento === "CNPJ" && digits.length !== 14) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["cpf_cnpj"],
        message: "CNPJ deve ter 14 dígitos.",
      });
    }

    if ((values.contrato.mensalidade ?? 0) <= 0) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["contrato", "mensalidade"],
        message: "A mensalidade deve ser maior que zero.",
      });
    }
  });

type FormValues = z.infer<typeof schema>;

type AssociadoFormProps = {
  mode: "create" | "edit";
  associadoId?: number;
  initialData?: AssociadoDetail | null;
  cancelHref?: string;
  successHref?: string | ((associado: AssociadoDetail) => string);
  title?: string;
  description?: string;
  submitLabel?: string;
  hideBackButton?: boolean;
  onSuccess?: (associado: AssociadoDetail) => Promise<void> | void;
};

type DocumentoValidationResponse = {
  exists: boolean;
  associado_id: number | null;
  agente_nome: string | null;
  message: string | null;
};

const stepTitles = [
  "Dados Cadastrais",
  "Endereço",
  "Dados Bancários",
  "Contato e Vínculo",
  "Contrato e Comprovantes",
];

const stepFields: Array<Array<keyof FormValues | string>> = [
  ["tipo_documento", "cpf_cnpj", "nome_completo"],
  [
    "endereco.cep",
    "endereco.endereco",
    "endereco.numero",
    "endereco.bairro",
    "endereco.cidade",
    "endereco.uf",
  ],
  [
    "dados_bancarios.banco",
    "dados_bancarios.agencia",
    "dados_bancarios.conta",
    "dados_bancarios.tipo_conta",
  ],
  [
    "contato.celular",
    "contato.email",
    "contato.orgao_publico",
    "contato.situacao_servidor",
    "contato.matricula_servidor",
  ],
  [
    "contrato.valor_bruto_total",
    "contrato.valor_liquido",
    "contrato.prazo_meses",
    "contrato.mensalidade",
  ],
];

function defaultValues(
  initialData?: AssociadoDetail | null,
  mode: "create" | "edit" = "create",
): FormValues {
  const contrato = initialData?.contratos?.[0];
  const today = startOfLocalDay(new Date());

  return {
    tipo_documento:
      (initialData?.tipo_documento as "CPF" | "CNPJ" | undefined) ?? "CPF",
    cpf_cnpj: initialData?.cpf_cnpj ?? "",
    rg: initialData?.rg ?? "",
    orgao_expedidor: initialData?.orgao_expedidor ?? "",
    nome_completo: initialData?.nome_completo ?? "",
    data_nascimento: parseIsoDate(initialData?.data_nascimento),
    profissao: initialData?.profissao ?? "",
    estado_civil: initialData?.estado_civil ?? "",
    status: initialData?.status ?? "cadastrado",
    status_contrato: contrato?.status ?? "rascunho",
    endereco: {
      cep: initialData?.endereco?.cep ?? "",
      endereco: initialData?.endereco?.endereco ?? "",
      numero: initialData?.endereco?.numero ?? "",
      complemento: initialData?.endereco?.complemento ?? "",
      bairro: initialData?.endereco?.bairro ?? "",
      cidade: initialData?.endereco?.cidade ?? "",
      uf: initialData?.endereco?.uf ?? "",
    },
    dados_bancarios: {
      banco: initialData?.dados_bancarios?.banco ?? "",
      agencia: initialData?.dados_bancarios?.agencia ?? "",
      conta: initialData?.dados_bancarios?.conta ?? "",
      tipo_conta: initialData?.dados_bancarios?.tipo_conta ?? "corrente",
      chave_pix: initialData?.dados_bancarios?.chave_pix ?? "",
    },
    contato: {
      celular: initialData?.contato?.celular ?? "",
      email: initialData?.contato?.email ?? "",
      orgao_publico:
        initialData?.contato?.orgao_publico ?? initialData?.orgao_publico ?? "",
      situacao_servidor: initialData?.contato?.situacao_servidor ?? "",
      matricula_servidor:
        initialData?.contato?.matricula_servidor ??
        initialData?.matricula_orgao ??
        "",
    },
    contrato: {
      valor_bruto_total: decimalToCents(contrato?.valor_bruto),
      valor_liquido: decimalToCents(contrato?.valor_liquido),
      prazo_meses: contrato?.prazo_meses ?? PRAZO_ANTECIPACAO_PADRAO,
      taxa_antecipacao:
        Number.parseFloat(
          contrato?.taxa_antecipacao ?? String(TAXA_ANTECIPACAO_PADRAO),
        ) || TAXA_ANTECIPACAO_PADRAO,
      mensalidade: decimalToCents(contrato?.valor_mensalidade),
      data_aprovacao:
        parseIsoDate(contrato?.data_aprovacao) ??
        (mode === "create" ? today : undefined),
    },
    agente_responsavel_id: initialData?.agente?.id ?? null,
    percentual_repasse:
      mode === "create"
        ? Number.parseFloat(initialData?.percentual_repasse ?? "10.00")
        : null,
  };
}

function toIsoDate(value?: Date) {
  if (!value) {
    return null;
  }

  const year = value.getFullYear();
  const month = String(value.getMonth() + 1).padStart(2, "0");
  const day = String(value.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

export default function AssociadoForm({
  mode,
  associadoId,
  initialData,
  cancelHref = "/associados",
  successHref = "/associados",
  title,
  description,
  submitLabel,
  hideBackButton = false,
  onSuccess,
}: AssociadoFormProps) {
  const router = useRouter();
  const { startRouteTransition } = useRouteTransition();
  const { user } = useAuth();
  const { hasRole } = usePermissions();
  const isAdminEditMode =
    mode === "edit" && (hasRole("ADMIN") || hasRole("COORDENADOR"));
  const canManageAgentAssignment =
    hasRole("ADMIN") ||
    hasRole("ANALISTA") ||
    hasRole("COORDENADOR") ||
    hasRole("TESOUREIRO");
  const canOverridePercentualRepasse =
    mode === "create" &&
    (hasRole("ADMIN") || hasRole("ANALISTA") || hasRole("COORDENADOR"));
  const [step, setStep] = React.useState(0);
  const [documentos, setDocumentos] = React.useState<
    Record<string, File | null>
  >({});
  const [isCheckingDocumento, setIsCheckingDocumento] = React.useState(false);
  const [isCompletingFlow, setIsCompletingFlow] = React.useState(false);
  const [adminMotivo, setAdminMotivo] = React.useState("");
  const [adminConfirmado, setAdminConfirmado] = React.useState(false);
  const todayRef = React.useRef(startOfLocalDay(new Date()));
  const documentoValidationCacheRef = React.useRef<{
    documento: string;
    isDuplicate: boolean;
    message: string | null;
  } | null>(null);
  const lastDuplicateAlertRef = React.useRef<string>("");

  const form = useForm<FormValues>({
    resolver: zodResolver(schema) as Resolver<FormValues>,
    defaultValues: defaultValues(initialData, mode),
    shouldUnregister: false,
  });

  const {
    register,
    control,
    reset,
    setValue,
    handleSubmit,
    trigger,
    getValues,
    getFieldState,
    setError,
    clearErrors,
    watch,
    formState: { errors, isSubmitting },
  } = form;

  React.useEffect(() => {
    reset(defaultValues(initialData, mode));
    documentoValidationCacheRef.current = null;
    lastDuplicateAlertRef.current = "";
  }, [initialData, mode, reset]);

  const valorBruto = watch("contrato.valor_bruto_total") ?? 0;
  const valorLiquido = watch("contrato.valor_liquido") ?? 0;
  const mensalidade = watch("contrato.mensalidade") ?? 0;
  const mensalidadeCalculada = mensalidade;
  const prazoMeses = watch("contrato.prazo_meses") ?? PRAZO_ANTECIPACAO_PADRAO;
  const taxaAntecipacao =
    watch("contrato.taxa_antecipacao") ?? TAXA_ANTECIPACAO_PADRAO;
  const dataAprovacao = watch("contrato.data_aprovacao");
  const tipoDocumento = watch("tipo_documento") ?? "CPF";
  const agenteResponsavelId = watch("agente_responsavel_id");

  const bruto30 = Math.round(valorBruto * 0.3);
  const valorTotalAntecipacao = mensalidadeCalculada * prazoMeses;
  const margemLiquido = Math.max(0, valorLiquido - bruto30);
  const margemDisponivel = Math.round(valorTotalAntecipacao * 0.7);
  const doacaoAssociado = valorTotalAntecipacao - margemDisponivel;
  const contratoAtual = initialData?.contratos?.[0];
  const currentDocumentsByType = React.useMemo(
    () => {
      const map = new Map<
        string,
        { latest: NonNullable<AssociadoDetail["documentos"]>[number]; total: number }
      >();
      for (const documento of initialData?.documentos ?? []) {
        const current = map.get(documento.tipo);
        if (!current) {
          map.set(documento.tipo, { latest: documento, total: 1 });
          continue;
        }
        current.total += 1;
        if (getDocumentoOrder(documento) >= getDocumentoOrder(current.latest)) {
          current.latest = documento;
        }
      }
      return map;
    },
    [initialData?.documentos],
  );
  const resolvedTitle =
    title ?? (mode === "create" ? "Novo Associado" : "Editar Associado");
  const resolvedDescription =
    description ??
    (mode === "create"
      ? "Cadastro multi-step com criação do contrato, ciclo inicial e entrada na esteira."
      : "Revise os dados cadastrais, contrato e comprovantes antes de salvar as alterações.");
  const resolvedSubmitLabel =
    submitLabel ??
    (mode === "create" ? "Enviar Cadastro" : "Salvar Alterações");
  const resolvedCancelHref =
    cancelHref === "/associados"
      ? hasRole("ANALISTA")
        ? "/analise/aptos"
        : hasRole("AGENTE")
          ? "/agentes/meus-contratos"
          : "/associados"
      : cancelHref;
  const agentesQuery = useQuery({
    queryKey: ["associado-form-agentes"],
    enabled: canManageAgentAssignment,
    queryFn: () => apiFetch<SimpleUser[]>("associados/agentes"),
  });
  const agentes = agentesQuery.data ?? [];
  const agenteResponsavelNome = React.useMemo(() => {
    if (canManageAgentAssignment) {
      const agenteSelecionado = agentes.find(
        (item) => item.id === agenteResponsavelId,
      );
      if (agenteSelecionado) {
        return agenteSelecionado.full_name;
      }
    }
    return (
      user?.full_name || initialData?.agente?.full_name || "Agente responsável"
    );
  }, [
    agenteResponsavelId,
    agentes,
    canManageAgentAssignment,
    initialData?.agente?.full_name,
    user?.full_name,
  ]);
  const contratoDates = React.useMemo(() => {
    if (mode === "create") {
      return calculateContratoDates(dataAprovacao, todayRef.current);
    }

    return {
      dataAprovacao,
      dataPrimeiraMensalidade:
        parseIsoDate(contratoAtual?.data_primeira_mensalidade) ??
        calculateContratoDates(dataAprovacao).dataPrimeiraMensalidade,
      mesAverbacao:
        parseIsoDate(contratoAtual?.mes_averbacao) ??
        calculateContratoDates(dataAprovacao).mesAverbacao,
    };
  }, [
    contratoAtual?.data_primeira_mensalidade,
    contratoAtual?.mes_averbacao,
    dataAprovacao,
    mode,
  ]);
  const parcelasPreview = React.useMemo(() => {
    const parcelasAtuais =
      mode === "edit"
        ? (contratoAtual?.ciclos ?? []).flatMap((ciclo) => ciclo.parcelas)
        : [];

    if (parcelasAtuais.length) {
      return parcelasAtuais.map((parcela) => ({
        numero: parcela.numero,
        referencia: parseIsoDate(parcela.referencia_mes) ?? new Date(),
        vencimento: parseIsoDate(parcela.data_vencimento) ?? new Date(),
        status: parcela.status,
        valor: decimalToCents(parcela.valor) ?? mensalidade,
      }));
    }

    if (!contratoDates.dataPrimeiraMensalidade) {
      return [];
    }

    const referenciaInicial = new Date(
      contratoDates.dataPrimeiraMensalidade.getFullYear(),
      contratoDates.dataPrimeiraMensalidade.getMonth(),
      1,
    );

    return Array.from({ length: prazoMeses }, (_, index) => {
      const referencia = addMonths(referenciaInicial, index);
      return {
        numero: index + 1,
        referencia,
        vencimento: new Date(
          referencia.getFullYear(),
          referencia.getMonth(),
          5,
        ),
        status: "pendente",
        valor: mensalidadeCalculada,
      };
    });
  }, [
    contratoAtual?.ciclos,
    contratoDates.dataPrimeiraMensalidade,
    mensalidadeCalculada,
    mode,
    prazoMeses,
  ]);

  React.useEffect(() => {
    if (mode !== "create") {
      return;
    }
    if (taxaAntecipacao !== TAXA_ANTECIPACAO_PADRAO) {
      setValue("contrato.taxa_antecipacao", TAXA_ANTECIPACAO_PADRAO, {
        shouldDirty: false,
        shouldValidate: false,
      });
    }

    if (!dataAprovacao) {
      setValue("contrato.data_aprovacao", todayRef.current, {
        shouldDirty: false,
        shouldValidate: false,
      });
    }
  }, [dataAprovacao, mode, setValue, taxaAntecipacao]);

  const mutation = useMutation({
    mutationFn: async (values: FormValues) => {
      const prazoPayload = values.contrato.prazo_meses;
      const taxaPayload =
        mode === "create"
          ? TAXA_ANTECIPACAO_PADRAO
          : values.contrato.taxa_antecipacao;
      const contractPayload = {
        valor_bruto_total: centsToDecimal(values.contrato.valor_bruto_total),
        valor_liquido: centsToDecimal(values.contrato.valor_liquido),
        prazo_meses: prazoPayload,
        taxa_antecipacao: taxaPayload.toFixed(2),
        mensalidade: centsToDecimal(values.contrato.mensalidade),
        margem_disponivel: centsToDecimal(margemDisponivel),
      };
      const payload = {
        tipo_documento: values.tipo_documento,
        cpf_cnpj: values.cpf_cnpj,
        rg: values.rg,
        orgao_expedidor: values.orgao_expedidor,
        nome_completo: values.nome_completo,
        data_nascimento: toIsoDate(values.data_nascimento),
        profissao: values.profissao,
        estado_civil: values.estado_civil,
        endereco: values.endereco,
        dados_bancarios: values.dados_bancarios,
        contato: values.contato,
        ...contractPayload,
        ...(mode === "create"
          ? {
              data_aprovacao: toIsoDate(
                contratoDates.dataAprovacao ?? values.contrato.data_aprovacao,
              ),
              data_primeira_mensalidade: toIsoDate(
                contratoDates.dataPrimeiraMensalidade,
              ),
              mes_averbacao: toIsoDate(contratoDates.mesAverbacao),
              doacao_associado: centsToDecimal(doacaoAssociado),
            }
          : {}),
        ...(canManageAgentAssignment
          ? {
              agente_responsavel_id: values.agente_responsavel_id,
            }
          : {}),
        ...(canOverridePercentualRepasse && values.percentual_repasse != null
          ? {
              percentual_repasse: values.percentual_repasse.toFixed(2),
            }
          : {}),
        ...(isAdminEditMode
          ? {
              updated_at: initialData?.updated_at ?? null,
              motivo: adminMotivo,
            }
          : {}),
      };

      const editAssociadoId = associadoId ?? initialData?.id;
      if (mode !== "create" && !editAssociadoId) {
        throw new Error("Associado inválido para edição.");
      }
      const associado =
        mode === "create"
          ? await apiFetch<AssociadoDetail>("associados", {
              method: "POST",
              body: payload,
            })
          : isAdminEditMode
            ? await (async () => {
                await apiFetch(
                  `admin-overrides/associados/${editAssociadoId}/core/`,
                  {
                    method: "POST",
                    body: {
                      updated_at: initialData?.updated_at ?? null,
                      motivo: adminMotivo,
                      nome_completo: values.nome_completo,
                      rg: values.rg,
                      orgao_expedidor: values.orgao_expedidor,
                      data_nascimento: toIsoDate(values.data_nascimento),
                      profissao: values.profissao,
                      estado_civil: values.estado_civil,
                      status: values.status,
                      status_contrato: values.status_contrato,
                      contrato_updated_at: null,
                      endereco: values.endereco,
                      dados_bancarios: values.dados_bancarios,
                      contato: values.contato,
                      ...contractPayload,
                      ...(canManageAgentAssignment
                        ? {
                            agente_responsavel_id: values.agente_responsavel_id,
                          }
                        : {}),
                    },
                  },
                );
                try {
                  return await apiFetch<AssociadoDetail>(
                    `associados/${editAssociadoId}`,
                  );
                } catch {
                  return {
                    ...(initialData ?? {}),
                    id: editAssociadoId,
                    nome_completo: values.nome_completo,
                  } as AssociadoDetail;
                }
              })()
            : await apiFetch<AssociadoDetail>(`associados/${editAssociadoId}`, {
                method: "PATCH",
                body: payload,
              });

      const uploads = Object.entries(documentos).filter(([, file]) => !!file);
      if (uploads.length) {
        await Promise.all(
          uploads.map(async ([tipo, file]) => {
            const formData = new FormData();
            formData.append("tipo", tipo);
            formData.append("arquivo", file as File);
            await apiFetch(`associados/${associado.id}/documentos`, {
              method: "POST",
              formData,
            });
          }),
        );
      }

      return associado;
    },
  });
  const isBusy =
    isSubmitting ||
    mutation.isPending ||
    isCheckingDocumento ||
    isCompletingFlow;

  async function validateDocumentoUnico(options?: { showToast?: boolean }) {
    if (mode !== "create") {
      return true;
    }

    const isFieldValid = await trigger("cpf_cnpj");
    if (!isFieldValid) {
      return false;
    }

    const documento = onlyDigits(getValues("cpf_cnpj"));
    if (!documento) {
      return false;
    }

    if (documentoValidationCacheRef.current?.documento === documento) {
      if (
        documentoValidationCacheRef.current.isDuplicate &&
        documentoValidationCacheRef.current.message
      ) {
        const message = documentoValidationCacheRef.current.message;
        setError("cpf_cnpj", { type: "manual", message });
        if (options?.showToast && lastDuplicateAlertRef.current !== documento) {
          toast.error(message);
          lastDuplicateAlertRef.current = documento;
        }
        return false;
      }

      clearErrors("cpf_cnpj");
      return true;
    }

    setIsCheckingDocumento(true);
    try {
      const response = await apiFetch<DocumentoValidationResponse>(
        "associados/validar-documento",
        { query: { cpf_cnpj: documento } },
      );

      if (documento !== onlyDigits(getValues("cpf_cnpj"))) {
        return false;
      }

      documentoValidationCacheRef.current = {
        documento,
        isDuplicate: response.exists,
        message: response.message,
      };

      if (response.exists && response.message) {
        setError("cpf_cnpj", { type: "manual", message: response.message });
        if (options?.showToast && lastDuplicateAlertRef.current !== documento) {
          toast.error(response.message);
          lastDuplicateAlertRef.current = documento;
        }
        return false;
      }

      clearErrors("cpf_cnpj");
      lastDuplicateAlertRef.current = "";
      return true;
    } finally {
      setIsCheckingDocumento(false);
    }
  }

  const nextStep = async () => {
    const valid = await trigger(stepFields[step] as never[], {
      shouldFocus: true,
    });
    if (!valid) return;
    if (step === 0) {
      const documentoValido = await validateDocumentoUnico({ showToast: true });
      if (!documentoValido) return;
    }
    setStep((current) => Math.min(stepTitles.length - 1, current + 1));
  };

  const previousStep = () => setStep((current) => Math.max(0, current - 1));
  const handleStepClick = (targetStep: number) => {
    if (isSubmitting || mutation.isPending) return;
    setStep(targetStep);
  };

  return (
    <form
      className="space-y-6"
      autoComplete="on"
      onSubmit={handleSubmit(async (values) => {
        // Guard: só processa o submit quando estiver na última etapa
        if (step !== stepTitles.length - 1) return;
        if (isAdminEditMode) {
          if (!adminMotivo.trim()) {
            toast.error("Informe o motivo da alteração administrativa.");
            return;
          }
          if (!adminConfirmado) {
            toast.error("Confirme a alteração administrativa antes de salvar.");
            return;
          }
        }
        if (canManageAgentAssignment && !values.agente_responsavel_id) {
          toast.error(
            "Selecione o agente responsável antes de enviar o cadastro.",
          );
          return;
        }
        try {
          const associado = await mutation.mutateAsync(values);
          setIsCompletingFlow(true);

          if (onSuccess) {
            await onSuccess(associado);
            return;
          }

          toast.success(
            mode === "create"
              ? "Associado cadastrado com sucesso."
              : "Associado atualizado com sucesso.",
          );
          const nextHref =
            typeof successHref === "function"
              ? successHref(associado)
              : mode === "create" && successHref === "/associados"
                ? `/associados/${associado.id}`
                : successHref;
          startRouteTransition(nextHref);
          router.push(nextHref);
          router.refresh();
        } catch (error) {
          toast.error(
            error instanceof Error
              ? error.message
              : "Falha ao salvar cadastro.",
          );
        } finally {
          setIsCompletingFlow(false);
        }
      })}
    >
      <Card className="glass-panel rounded-[2rem] border-border/60 shadow-xl shadow-black/20">
        <CardHeader className="space-y-5">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <CardTitle className="text-2xl">{resolvedTitle}</CardTitle>
              <p className="mt-2 text-sm text-muted-foreground">
                {resolvedDescription}
              </p>
            </div>
            {!hideBackButton ? (
              <Button variant="outline" asChild>
                <Link href={resolvedCancelHref}>Voltar</Link>
              </Button>
            ) : null}
          </div>
          <div className="grid gap-3 md:grid-cols-5">
            {stepTitles.map((title, index) => (
              <button
                type="button"
                key={title}
                onClick={() => handleStepClick(index)}
                disabled={isBusy}
                className={`rounded-2xl border px-4 py-3 text-sm ${
                  index === step
                    ? "border-primary/60 bg-primary/10 text-foreground"
                    : index < step
                      ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-200"
                      : "border-border/60 bg-card/60 text-muted-foreground"
                } ${
                  !isBusy
                    ? "cursor-pointer text-left transition hover:border-primary/60 hover:bg-primary/5"
                    : "cursor-not-allowed text-left"
                }`}
                aria-current={index === step ? "step" : undefined}
              >
                <div className="mb-1 flex items-center gap-2 text-xs uppercase tracking-[0.18em]">
                  {index < step ? <CheckIcon className="size-3" /> : null}
                  Etapa {index + 1}
                </div>
                <p className="font-medium">{title}</p>
              </button>
            ))}
          </div>
        </CardHeader>
        <CardContent className="space-y-6">
          {step === 0 ? (
            <FieldGroup className="grid gap-5 md:grid-cols-2">
              <Field>
                <FieldLabel>Tipo de documento</FieldLabel>
                <FieldContent>
                  <Controller
                    control={control}
                    name="tipo_documento"
                    render={({ field }) => (
                      <Select
                        value={field.value}
                        onValueChange={field.onChange}
                      >
                        <SelectTrigger className="w-full rounded-xl bg-card/60">
                          <SelectValue placeholder="Selecione" />
                        </SelectTrigger>
                        <SelectContent>
                          <SelectItem value="CPF">CPF</SelectItem>
                          <SelectItem value="CNPJ">CNPJ</SelectItem>
                        </SelectContent>
                      </Select>
                    )}
                  />
                  <FieldError errors={[errors.tipo_documento]} />
                </FieldContent>
              </Field>
              <Field>
                <FieldLabel>CPF/CNPJ</FieldLabel>
                <FieldContent>
                  <Controller
                    control={control}
                    name="cpf_cnpj"
                    render={({ field }) => (
                      <InputCpfCnpj
                        id={tipoDocumento === "CPF" ? "cpf" : "cnpj"}
                        name={tipoDocumento === "CPF" ? "cpf" : "cnpj"}
                        value={field.value}
                        autoComplete="on"
                        onChange={(nextValue) => {
                          documentoValidationCacheRef.current = null;
                          lastDuplicateAlertRef.current = "";
                          if (
                            getFieldState("cpf_cnpj").error?.type === "manual"
                          ) {
                            clearErrors("cpf_cnpj");
                          }
                          field.onChange(nextValue);
                        }}
                        onBlur={() => {
                          field.onBlur();
                          void validateDocumentoUnico({ showToast: true });
                        }}
                        aria-invalid={!!errors.cpf_cnpj}
                        className="rounded-xl bg-card/60"
                      />
                    )}
                  />
                  <FieldError errors={[errors.cpf_cnpj]} />
                </FieldContent>
              </Field>
              <Field>
                <FieldLabel>RG</FieldLabel>
                <FieldContent>
                  <Input
                    {...register("rg")}
                    autoComplete="off"
                    className="rounded-xl bg-card/60"
                  />
                </FieldContent>
              </Field>
              <Field>
                <FieldLabel>Órgão expedidor</FieldLabel>
                <FieldContent>
                  <Input
                    {...register("orgao_expedidor")}
                    autoComplete="off"
                    className="rounded-xl bg-card/60"
                  />
                </FieldContent>
              </Field>
              <Field className="md:col-span-2">
                <FieldLabel>Nome completo / razão social</FieldLabel>
                <FieldContent>
                  <Input
                    {...register("nome_completo")}
                    autoComplete="name"
                    className="rounded-xl bg-card/60"
                  />
                  <FieldError errors={[errors.nome_completo]} />
                </FieldContent>
              </Field>
              <Field>
                <FieldLabel>Data de nascimento</FieldLabel>
                <FieldContent>
                  <Controller
                    control={control}
                    name="data_nascimento"
                    render={({ field }) => (
                      <DatePicker
                        value={field.value}
                        onChange={field.onChange}
                      />
                    )}
                  />
                  <FieldError errors={[errors.data_nascimento]} />
                </FieldContent>
              </Field>
              <Field>
                <FieldLabel>Profissão</FieldLabel>
                <FieldContent>
                  <Input
                    {...register("profissao")}
                    autoComplete="organization-title"
                    className="rounded-xl bg-card/60"
                  />
                </FieldContent>
              </Field>
              <Field>
                <FieldLabel>Estado civil</FieldLabel>
                <FieldContent>
                  <Controller
                    control={control}
                    name="estado_civil"
                    render={({ field }) => (
                      <Select
                        value={field.value}
                        onValueChange={field.onChange}
                      >
                        <SelectTrigger className="w-full rounded-xl bg-card/60">
                          <SelectValue placeholder="Selecione" />
                        </SelectTrigger>
                        <SelectContent>
                          {estadoCivilOptions.map((option) => (
                            <SelectItem key={option.value} value={option.value}>
                              {option.label}
                            </SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                    )}
                  />
                </FieldContent>
              </Field>
              {isAdminEditMode ? (
                <>
                  <Field>
                    <FieldLabel>Status do associado</FieldLabel>
                    <FieldContent>
                      <Controller
                        control={control}
                        name="status"
                        render={({ field }) => (
                          <Select
                            value={field.value || "cadastrado"}
                            onValueChange={field.onChange}
                          >
                            <SelectTrigger className="w-full rounded-xl bg-card/60">
                              <SelectValue placeholder="Selecione o status" />
                            </SelectTrigger>
                            <SelectContent>
                              {associadoStatusOptions.map((option) => (
                                <SelectItem
                                  key={option.value}
                                  value={option.value}
                                >
                                  {option.label}
                                </SelectItem>
                              ))}
                            </SelectContent>
                          </Select>
                        )}
                      />
                    </FieldContent>
                  </Field>
                  <Field>
                    <FieldLabel>Status do contrato</FieldLabel>
                    <FieldContent>
                      <Controller
                        control={control}
                        name="status_contrato"
                        render={({ field }) => (
                          <Select
                            value={field.value || "rascunho"}
                            onValueChange={field.onChange}
                          >
                            <SelectTrigger className="w-full rounded-xl bg-card/60">
                              <SelectValue placeholder="Selecione o status" />
                            </SelectTrigger>
                            <SelectContent>
                              {contratoStatusOptions.map((option) => (
                                <SelectItem
                                  key={option.value}
                                  value={option.value}
                                >
                                  {option.label}
                                </SelectItem>
                              ))}
                            </SelectContent>
                          </Select>
                        )}
                      />
                    </FieldContent>
                  </Field>
                </>
              ) : null}
            </FieldGroup>
          ) : null}

          {step === 1 ? (
            <FieldGroup className="grid gap-5 md:grid-cols-2">
              <Field>
                <FieldLabel>CEP</FieldLabel>
                <FieldContent>
                  <Controller
                    control={control}
                    name="endereco.cep"
                    render={({ field }) => (
                      <InputCep
                        name={field.name}
                        value={field.value}
                        autoComplete="postal-code"
                        onChange={field.onChange}
                        onAddressResolved={(address) => {
                          if (!address) return;
                          setValue("endereco.endereco", address.logradouro);
                          setValue("endereco.bairro", address.bairro);
                          setValue("endereco.cidade", address.localidade);
                          setValue("endereco.uf", address.uf);
                          if (address.complemento) {
                            setValue(
                              "endereco.complemento",
                              address.complemento,
                            );
                          }
                        }}
                        className="rounded-xl bg-card/60"
                      />
                    )}
                  />
                  <FieldError errors={[errors.endereco?.cep]} />
                </FieldContent>
              </Field>
              <Field className="md:col-span-2">
                <FieldLabel>Endereço</FieldLabel>
                <FieldContent>
                  <Input
                    {...register("endereco.endereco")}
                    autoComplete="street-address"
                    className="rounded-xl bg-card/60"
                  />
                  <FieldError errors={[errors.endereco?.endereco]} />
                </FieldContent>
              </Field>
              <Field>
                <FieldLabel>Número</FieldLabel>
                <FieldContent>
                  <Input
                    {...register("endereco.numero")}
                    autoComplete="address-line2"
                    placeholder="Opcional / S/N"
                    className="rounded-xl bg-card/60"
                  />
                  <FieldError errors={[errors.endereco?.numero]} />
                </FieldContent>
              </Field>
              <Field>
                <FieldLabel>Complemento</FieldLabel>
                <FieldContent>
                  <Input
                    {...register("endereco.complemento")}
                    autoComplete="address-line3"
                    className="rounded-xl bg-card/60"
                  />
                </FieldContent>
              </Field>
              <Field>
                <FieldLabel>Bairro</FieldLabel>
                <FieldContent>
                  <Input
                    {...register("endereco.bairro")}
                    autoComplete="address-level3"
                    className="rounded-xl bg-card/60"
                  />
                  <FieldError errors={[errors.endereco?.bairro]} />
                </FieldContent>
              </Field>
              <Field>
                <FieldLabel>Cidade</FieldLabel>
                <FieldContent>
                  <Input
                    {...register("endereco.cidade")}
                    autoComplete="address-level2"
                    className="rounded-xl bg-card/60"
                  />
                  <FieldError errors={[errors.endereco?.cidade]} />
                </FieldContent>
              </Field>
              <Field>
                <FieldLabel>UF</FieldLabel>
                <FieldContent>
                  <Controller
                    control={control}
                    name="endereco.uf"
                    render={({ field }) => (
                      <Select
                        value={field.value}
                        onValueChange={field.onChange}
                      >
                        <SelectTrigger className="w-full rounded-xl bg-card/60">
                          <SelectValue placeholder="Selecione" />
                        </SelectTrigger>
                        <SelectContent>
                          {ufOptions.map((uf) => (
                            <SelectItem key={uf} value={uf}>
                              {uf}
                            </SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                    )}
                  />
                  <FieldError errors={[errors.endereco?.uf]} />
                </FieldContent>
              </Field>
            </FieldGroup>
          ) : null}

          {step === 2 ? (
            <FieldGroup className="grid gap-5 md:grid-cols-2">
              <Field>
                <FieldLabel>Banco</FieldLabel>
                <FieldContent>
                  <Input
                    {...register("dados_bancarios.banco")}
                    placeholder="Digite o banco"
                    className="rounded-xl bg-card/60"
                  />
                  <FieldError errors={[errors.dados_bancarios?.banco]} />
                </FieldContent>
              </Field>
              <Field>
                <FieldLabel>Agência</FieldLabel>
                <FieldContent>
                  <Input
                    {...register("dados_bancarios.agencia")}
                    className="rounded-xl bg-card/60"
                  />
                  <FieldError errors={[errors.dados_bancarios?.agencia]} />
                </FieldContent>
              </Field>
              <Field>
                <FieldLabel>Conta</FieldLabel>
                <FieldContent>
                  <Input
                    {...register("dados_bancarios.conta")}
                    className="rounded-xl bg-card/60"
                  />
                  <FieldError errors={[errors.dados_bancarios?.conta]} />
                </FieldContent>
              </Field>
              <Field>
                <FieldLabel>Tipo de conta</FieldLabel>
                <FieldContent>
                  <Controller
                    control={control}
                    name="dados_bancarios.tipo_conta"
                    render={({ field }) => (
                      <Select
                        value={field.value}
                        onValueChange={field.onChange}
                      >
                        <SelectTrigger className="w-full rounded-xl bg-card/60">
                          <SelectValue placeholder="Selecione" />
                        </SelectTrigger>
                        <SelectContent>
                          <SelectItem value="corrente">Corrente</SelectItem>
                          <SelectItem value="poupanca">Poupança</SelectItem>
                          <SelectItem value="salario">Salário</SelectItem>
                        </SelectContent>
                      </Select>
                    )}
                  />
                  <FieldError errors={[errors.dados_bancarios?.tipo_conta]} />
                </FieldContent>
              </Field>
              <Field className="md:col-span-2">
                <FieldLabel>Chave PIX</FieldLabel>
                <FieldContent>
                  <Controller
                    control={control}
                    name="dados_bancarios.chave_pix"
                    render={({ field }) => (
                      <InputPixKey
                        name={field.name}
                        value={field.value}
                        autoComplete="off"
                        onChange={field.onChange}
                        className="rounded-xl bg-card/60"
                      />
                    )}
                  />
                </FieldContent>
              </Field>
            </FieldGroup>
          ) : null}

          {step === 3 ? (
            <FieldGroup className="grid gap-5 md:grid-cols-2">
              <Field>
                <FieldLabel>Celular</FieldLabel>
                <FieldContent>
                  <Controller
                    control={control}
                    name="contato.celular"
                    render={({ field }) => (
                      <InputPhone
                        name={field.name}
                        value={field.value}
                        autoComplete="tel-national"
                        onChange={field.onChange}
                        className="rounded-xl bg-card/60"
                      />
                    )}
                  />
                  <FieldError errors={[errors.contato?.celular]} />
                </FieldContent>
              </Field>
              <Field>
                <FieldLabel>E-mail</FieldLabel>
                <FieldContent>
                  <Input
                    {...register("contato.email")}
                    type="email"
                    autoComplete="email"
                    className="rounded-xl bg-card/60"
                  />
                  <FieldError errors={[errors.contato?.email]} />
                </FieldContent>
              </Field>
              <Field>
                <FieldLabel>Órgão público</FieldLabel>
                <FieldContent>
                  <Input
                    {...register("contato.orgao_publico")}
                    autoComplete="organization"
                    placeholder="Digite o órgão público"
                    className="rounded-xl bg-card/60"
                  />
                  <FieldError errors={[errors.contato?.orgao_publico]} />
                </FieldContent>
              </Field>
              <Field>
                <FieldLabel>Situação do servidor</FieldLabel>
                <FieldContent>
                  <Controller
                    control={control}
                    name="contato.situacao_servidor"
                    render={({ field }) => (
                      <Select
                        value={field.value}
                        onValueChange={field.onChange}
                      >
                        <SelectTrigger className="w-full rounded-xl bg-card/60">
                          <SelectValue placeholder="Selecione" />
                        </SelectTrigger>
                        <SelectContent>
                          {situacaoServidorOptions.map((option) => (
                            <SelectItem key={option.value} value={option.value}>
                              {option.label}
                            </SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                    )}
                  />
                  <FieldError errors={[errors.contato?.situacao_servidor]} />
                </FieldContent>
              </Field>
              <Field className="md:col-span-2">
                <FieldLabel>Matrícula do servidor público</FieldLabel>
                <FieldContent>
                  <Input
                    {...register("contato.matricula_servidor")}
                    autoComplete="off"
                    className="rounded-xl bg-card/60"
                  />
                  <FieldError errors={[errors.contato?.matricula_servidor]} />
                </FieldContent>
              </Field>
            </FieldGroup>
          ) : null}

          {step === 4 ? (
            <div className="space-y-4">
              <Card className="rounded-[1.5rem] border-border/60 bg-card/50">
                <CardHeader className="space-y-2">
                  <CardTitle className="text-base">
                    Dados para cálculo de margem (pré-validação)
                  </CardTitle>
                  <p className="text-sm text-muted-foreground">
                    Preencha apenas os valores-base. O restante do cálculo é
                    preenchido automaticamente.
                  </p>
                </CardHeader>
                <CardContent>
                  <FieldGroup className="grid gap-5 md:grid-cols-5">
                    <Field>
                      <FieldLabel>Valor bruto total</FieldLabel>
                      <FieldContent>
                        <Controller
                          control={control}
                          name="contrato.valor_bruto_total"
                          render={({ field }) => (
                            <InputCurrency
                              value={field.value}
                              onChange={field.onChange}
                            />
                          )}
                        />
                        <FieldError
                          errors={[errors.contrato?.valor_bruto_total]}
                        />
                      </FieldContent>
                    </Field>
                    <Field>
                      <FieldLabel>Valor líquido (contra-cheque)</FieldLabel>
                      <FieldContent>
                        <Controller
                          control={control}
                          name="contrato.valor_liquido"
                          render={({ field }) => (
                            <InputCurrency
                              value={field.value}
                              onChange={field.onChange}
                            />
                          )}
                        />
                        <FieldError errors={[errors.contrato?.valor_liquido]} />
                      </FieldContent>
                    </Field>
                    <Field>
                      <FieldLabel>Prazo de antecipação (meses)</FieldLabel>
                      <FieldContent>
                        <Controller
                          control={control}
                          name="contrato.prazo_meses"
                          render={({ field }) => (
                            <Select
                              value={String(
                                field.value ?? PRAZO_ANTECIPACAO_PADRAO,
                              )}
                              onValueChange={(value) =>
                                field.onChange(Number.parseInt(value, 10))
                              }
                            >
                              <SelectTrigger className="w-full rounded-xl bg-card/60">
                                <SelectValue placeholder="Prazo do ciclo" />
                              </SelectTrigger>
                              <SelectContent>
                                {PRAZO_ANTECIPACAO_OPTIONS.map((option) => (
                                  <SelectItem
                                    key={option.value}
                                    value={option.value}
                                  >
                                    {option.label}
                                  </SelectItem>
                                ))}
                              </SelectContent>
                            </Select>
                          )}
                        />
                        <FieldError errors={[errors.contrato?.prazo_meses]} />
                      </FieldContent>
                    </Field>
                    <Field>
                      <FieldLabel>30% do bruto</FieldLabel>
                      <FieldContent>
                        <Input
                          readOnly
                          aria-readonly="true"
                          value={formatCurrency(bruto30 / 100)}
                          className="rounded-xl bg-card/60"
                        />
                      </FieldContent>
                    </Field>
                    <Field>
                      <FieldLabel>Margem (líquido - 30% do bruto)</FieldLabel>
                      <FieldContent>
                        <Input
                          readOnly
                          aria-readonly="true"
                          value={formatCurrency(margemLiquido / 100)}
                          className="rounded-xl bg-card/60"
                        />
                      </FieldContent>
                    </Field>
                  </FieldGroup>
                </CardContent>
              </Card>

              <Card className="rounded-[1.5rem] border-border/60 bg-card/50">
                <CardHeader className="space-y-2">
                  <CardTitle className="text-base">
                    Detalhes do contrato
                  </CardTitle>
                  <p className="text-sm text-muted-foreground">
                    Mensalidade digitada manualmente e demais campos calculados
                    automaticamente a partir da regra do contrato.
                  </p>
                </CardHeader>
                <CardContent>
                  <FieldGroup className="grid gap-5 md:grid-cols-4">
                    <Field>
                      <FieldLabel>Mensalidade associativa (R$)</FieldLabel>
                      <FieldContent>
                        <Controller
                          control={control}
                          name="contrato.mensalidade"
                          render={({ field }) => (
                            <InputCurrency
                              value={field.value}
                              onChange={field.onChange}
                            />
                          )}
                        />
                        <FieldError errors={[errors.contrato?.mensalidade]} />
                      </FieldContent>
                    </Field>
                    <Field>
                      <FieldLabel>Taxa de antecipação (%)</FieldLabel>
                      <FieldContent>
                        <Controller
                          control={control}
                          name="contrato.taxa_antecipacao"
                          render={({ field }) => (
                            <Input
                              readOnly
                              aria-readonly="true"
                              value={`${Number(field.value ?? TAXA_ANTECIPACAO_PADRAO).toFixed(2)}%`}
                              className="rounded-xl bg-card/60"
                            />
                          )}
                        />
                      </FieldContent>
                    </Field>
                    <Field>
                      <FieldLabel>Disponível (R$)</FieldLabel>
                      <FieldContent>
                        <Input
                          readOnly
                          aria-readonly="true"
                          value={formatCurrency(margemDisponivel / 100)}
                          className="rounded-xl bg-card/60"
                        />
                      </FieldContent>
                    </Field>
                    <Field>
                      <FieldLabel>Valor total antecipação (R$)</FieldLabel>
                      <FieldContent>
                        <Input
                          readOnly
                          aria-readonly="true"
                          value={formatCurrency(valorTotalAntecipacao / 100)}
                          className="rounded-xl bg-card/60"
                        />
                      </FieldContent>
                    </Field>
                    <Field>
                      <FieldLabel>Data da aprovação</FieldLabel>
                      <FieldContent>
                        <Controller
                          control={control}
                          name="contrato.data_aprovacao"
                          render={({ field }) => (
                            <Input
                              readOnly
                              aria-readonly="true"
                              value={
                                field.value ? formatMonthDate(field.value) : ""
                              }
                              className="rounded-xl bg-card/60"
                            />
                          )}
                        />
                      </FieldContent>
                    </Field>
                    <Field>
                      <FieldLabel>Data da primeira mensalidade</FieldLabel>
                      <FieldContent>
                        <Input
                          readOnly
                          aria-readonly="true"
                          value={
                            contratoDates.dataPrimeiraMensalidade
                              ? formatMonthDate(
                                  contratoDates.dataPrimeiraMensalidade,
                                )
                              : ""
                          }
                          className="rounded-xl bg-card/60"
                        />
                      </FieldContent>
                    </Field>
                    <Field>
                      <FieldLabel>Status</FieldLabel>
                      <FieldContent>
                        <Input
                          readOnly
                          aria-readonly="true"
                          value={resolveContratoStatusLabel(
                            contratoAtual?.status,
                          )}
                          className="rounded-xl bg-card/60"
                        />
                      </FieldContent>
                    </Field>
                    <Field>
                      <FieldLabel>Mês de averbação</FieldLabel>
                      <FieldContent>
                        <Input
                          readOnly
                          aria-readonly="true"
                          value={formatLongMonthYear(
                            contratoDates.mesAverbacao,
                          )}
                          className="rounded-xl bg-card/60"
                        />
                      </FieldContent>
                    </Field>
                    <Field className="md:col-span-2">
                      <FieldLabel>Doação do associado (R$)</FieldLabel>
                      <FieldContent>
                        <Input
                          readOnly
                          aria-readonly="true"
                          value={formatCurrency(doacaoAssociado / 100)}
                          className="rounded-xl bg-card/60"
                        />
                      </FieldContent>
                    </Field>
                  </FieldGroup>
                </CardContent>
              </Card>

              <div className="space-y-4">
                <Card className="rounded-[1.5rem] border-border/60 bg-card/50">
                  <CardHeader className="space-y-2">
                    <CardTitle className="text-base">
                      Antecipações (opcional)
                    </CardTitle>
                    <p className="text-sm text-muted-foreground">
                      As linhas abaixo são preenchidas automaticamente conforme
                      a mensalidade e a data da primeira cobrança.
                    </p>
                  </CardHeader>
                  <CardContent className="space-y-4">
                    {parcelasPreview.map((parcela) => (
                      <div
                        key={`${parcela.numero}-${parcela.referencia.toISOString()}`}
                        className="rounded-3xl border border-border/60 bg-background/40 p-4"
                      >
                        <p className="text-base font-semibold">
                          Linha {parcela.numero}
                        </p>
                        <FieldGroup className="mt-4 grid gap-5 md:grid-cols-4">
                          <Field>
                            <FieldLabel>Nº Mensalidade</FieldLabel>
                            <FieldContent>
                              <Input
                                readOnly
                                aria-readonly="true"
                                value={String(parcela.numero)}
                                className="rounded-xl bg-card/60"
                              />
                            </FieldContent>
                          </Field>
                          <Field>
                            <FieldLabel>Valor (R$)</FieldLabel>
                            <FieldContent>
                              <Input
                                readOnly
                                aria-readonly="true"
                                value={formatCurrency(parcela.valor / 100)}
                                className="rounded-xl bg-card/60"
                              />
                            </FieldContent>
                          </Field>
                          <Field>
                            <FieldLabel>Vencimento</FieldLabel>
                            <FieldContent>
                              <Input
                                readOnly
                                aria-readonly="true"
                                value={formatMonthDate(parcela.vencimento)}
                                className="rounded-xl bg-card/60"
                              />
                            </FieldContent>
                          </Field>
                          <Field>
                            <FieldLabel>Status</FieldLabel>
                            <FieldContent>
                              <Input
                                readOnly
                                aria-readonly="true"
                                value={resolveParcelaStatusLabel(
                                  parcela.status,
                                )}
                                className="rounded-xl bg-card/60"
                              />
                            </FieldContent>
                          </Field>
                        </FieldGroup>
                      </div>
                    ))}
                  </CardContent>
                </Card>

                <Card className="rounded-[1.5rem] border-border/60 bg-card/50">
                  <CardHeader className="space-y-2">
                    <CardTitle className="text-base">
                      Agente Responsável
                    </CardTitle>
                  </CardHeader>
                  <CardContent>
                    <FieldGroup className="grid gap-5">
                      <Field>
                        <FieldLabel>Agente responsável</FieldLabel>
                        <FieldContent>
                          {canManageAgentAssignment ? (
                            <Controller
                              control={control}
                              name="agente_responsavel_id"
                              render={({ field }) => (
                                <Select
                                  value={field.value ? String(field.value) : ""}
                                  onValueChange={(value) =>
                                    field.onChange(
                                      value ? Number.parseInt(value, 10) : null,
                                    )
                                  }
                                >
                                  <SelectTrigger className="w-full rounded-xl bg-card/60">
                                    <SelectValue
                                      placeholder={
                                        agentesQuery.isLoading
                                          ? "Carregando agentes..."
                                          : "Selecione o agente"
                                      }
                                    />
                                  </SelectTrigger>
                                  <SelectContent>
                                    {agentes.map((agente) => (
                                      <SelectItem
                                        key={agente.id}
                                        value={String(agente.id)}
                                      >
                                        {agente.full_name}
                                      </SelectItem>
                                    ))}
                                  </SelectContent>
                                </Select>
                              )}
                            />
                          ) : (
                            <Input
                              readOnly
                              aria-readonly="true"
                              value={agenteResponsavelNome}
                              className="rounded-xl bg-card/60"
                            />
                          )}
                        </FieldContent>
                      </Field>
                      {canOverridePercentualRepasse ? (
                        <Field>
                          <FieldLabel>Percentual de repasse (%)</FieldLabel>
                          <FieldContent>
                            <Controller
                              control={control}
                              name="percentual_repasse"
                              render={({ field }) => (
                                <Input
                                  type="number"
                                  min="0"
                                  step="0.01"
                                  value={field.value ?? ""}
                                  onChange={(event) => {
                                    const value = event.target.value;
                                    field.onChange(
                                      value === ""
                                        ? null
                                        : Number.parseFloat(value),
                                    );
                                  }}
                                  placeholder="Ex.: 10.00"
                                  className="rounded-xl bg-card/60"
                                />
                              )}
                            />
                            <FieldError errors={[errors.percentual_repasse]} />
                          </FieldContent>
                        </Field>
                      ) : null}
                      <div className="rounded-2xl border border-border/60 bg-background/40 px-4 py-3 text-sm text-muted-foreground">
                        {canOverridePercentualRepasse
                          ? "O percentual informado vale apenas para este novo cadastro e não altera associados já ativos."
                          : "A comissão do agente segue a configuração automática definida na tesouraria."}
                      </div>
                    </FieldGroup>
                  </CardContent>
                </Card>

                <Card className="rounded-[1.5rem] border-border/60 bg-card/50">
                  <CardHeader className="space-y-2">
                    <CardTitle className="text-base">
                      Documentos (imagem ou PDF - até 100 MB cada)
                    </CardTitle>
                    <p className="text-sm text-muted-foreground">
                      {mode === "edit"
                        ? "Revise os anexos atuais e envie um novo arquivo para adicionar uma versão sem apagar o histórico."
                        : "Os arquivos enviados aqui são anexados após o cadastro principal."}
                    </p>
                  </CardHeader>
                  <CardContent>
                    <div className="grid gap-4 md:grid-cols-4">
                      {documentFields.map((field) => {
                        const currentDocumentEntry = currentDocumentsByType.get(
                          field.key,
                        );
                        const currentDocument = currentDocumentEntry?.latest;

                        return (
                          <div
                            key={field.key}
                            className="space-y-3 rounded-3xl border border-border/60 bg-card/40 p-4"
                          >
                            <p className="text-sm font-medium">{field.label}</p>
                            {mode === "edit" ? (
                              currentDocument ? (
                                <div className="rounded-2xl border border-border/60 bg-background/60 p-3">
                                  <div className="flex items-center justify-between gap-3">
                                    <a
                                      href={buildBackendFileUrl(
                                        currentDocument.arquivo,
                                      )}
                                      target="_blank"
                                      rel="noreferrer"
                                      className="text-sm font-medium underline-offset-4 hover:underline"
                                    >
                                      Abrir arquivo atual
                                    </a>
                                    <StatusBadge
                                      status={currentDocument.status}
                                    />
                                  </div>
                                  <p className="mt-2 text-xs text-muted-foreground">
                                    Envie um novo arquivo abaixo para adicionar
                                    uma nova versão mantendo este anexo no
                                    histórico.
                                    {currentDocumentEntry.total > 1
                                      ? ` Histórico atual: ${currentDocumentEntry.total} versões.`
                                      : ""}
                                  </p>
                                </div>
                              ) : (
                                <div className="rounded-2xl border border-dashed border-border/60 bg-background/40 p-3 text-xs text-muted-foreground">
                                  Nenhum arquivo atual para este tipo.
                                </div>
                              )
                            ) : null}
                            <FileUploadDropzone
                              file={documentos[field.key] ?? null}
                              maxSize={100 * 1024 * 1024}
                              onUpload={(file) => {
                                setDocumentos((current) => ({
                                  ...current,
                                  [field.key]: file,
                                }));
                              }}
                              emptyDescription="Qualquer tipo de arquivo, com limite de 100 MB."
                              className="min-h-36 px-4 py-6"
                            />
                          </div>
                        );
                      })}
                    </div>
                  </CardContent>
                </Card>
              </div>
            </div>
          ) : null}
        </CardContent>
      </Card>

      {isAdminEditMode ? (
        <Card className="glass-panel rounded-[2rem] border-primary/20 bg-primary/5 shadow-xl shadow-black/20">
          <CardHeader>
            <CardTitle className="text-2xl">
              Confirmação Administrativa
            </CardTitle>
            <p className="mt-2 text-sm text-muted-foreground">
              Esta alteração será gravada pelo fluxo admin auditado e ficará
              registrada no histórico do associado.
            </p>
          </CardHeader>
          <CardContent className="space-y-4">
            <Field>
              <FieldLabel>Motivo da alteração</FieldLabel>
              <FieldContent>
                <Textarea
                  rows={4}
                  value={adminMotivo}
                  onChange={(event) => setAdminMotivo(event.target.value)}
                  placeholder="Explique por que este cadastro está sendo ajustado pelo admin."
                />
              </FieldContent>
            </Field>
            <label className="flex items-start gap-3 rounded-2xl border border-border/60 bg-background/60 p-4 text-sm">
              <Checkbox
                checked={adminConfirmado}
                onCheckedChange={(checked) =>
                  setAdminConfirmado(checked === true)
                }
              />
              <span>
                Confirmo que esta alteração deve sobrescrever o cadastro atual e
                permanecer no histórico administrativo.
              </span>
            </label>
          </CardContent>
        </Card>
      ) : null}

      <div className="flex items-center justify-between gap-3">
        <Button
          type="button"
          variant="outline"
          onClick={previousStep}
          disabled={step === 0 || isBusy}
        >
          <ArrowLeftIcon className="size-4" />
          Voltar
        </Button>
        {step < stepTitles.length - 1 ? (
          <Button
            key="btn-next"
            type="button"
            onClick={nextStep}
            disabled={isBusy}
          >
            Próximo
            <ArrowRightIcon className="size-4" />
          </Button>
        ) : (
          <Button key="btn-submit" type="submit" disabled={isBusy}>
            <SaveIcon className="size-4" />
            {resolvedSubmitLabel}
          </Button>
        )}
      </div>
    </form>
  );
}

function resolveContratoStatusLabel(status?: string) {
  switch (status) {
    case "rascunho":
      return "Rascunho";
    case "ativo":
      return "Ativo";
    case "encerrado":
      return "Encerrado";
    case "cancelado":
      return "Cancelado";
    case "em_analise":
    default:
      return "Pendente";
  }
}

function resolveParcelaStatusLabel(status?: string) {
  switch (status) {
    case "futuro":
      return "Futuro";
    case "descontado":
      return "Descontado";
    case "nao_descontado":
      return "Nao descontado";
    case "cancelado":
      return "Cancelado";
    case "em_aberto":
    case "pendente":
    default:
      return "Pendente";
  }
}

function formatMonthDate(value: Date) {
  const day = String(value.getDate()).padStart(2, "0");
  const month = String(value.getMonth() + 1).padStart(2, "0");
  return `${day}/${month}/${value.getFullYear()}`;
}
