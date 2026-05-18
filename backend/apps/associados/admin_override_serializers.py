from __future__ import annotations

from rest_framework import serializers

from apps.associados.serializers import (
    ContatoHistoricoSerializer,
    DadosBancariosSerializer,
    EnderecoSerializer,
    SimpleUserSerializer,
)
from apps.contratos.serializers import ProjectedComprovanteSerializer


class AdminOverrideChangeReadSerializer(serializers.Serializer):
    id = serializers.IntegerField(read_only=True)
    entity_type = serializers.CharField(read_only=True)
    entity_id = serializers.IntegerField(read_only=True)
    competencia_referencia = serializers.DateField(read_only=True, allow_null=True)
    resumo = serializers.CharField(read_only=True)
    before_snapshot = serializers.JSONField(read_only=True)
    after_snapshot = serializers.JSONField(read_only=True)


class AdminOverrideEventReadSerializer(serializers.Serializer):
    id = serializers.IntegerField(read_only=True)
    escopo = serializers.CharField(read_only=True)
    resumo = serializers.CharField(read_only=True)
    motivo = serializers.CharField(read_only=True)
    confirmacao_dupla = serializers.BooleanField(read_only=True)
    created_at = serializers.DateTimeField(read_only=True)
    realizado_por = SimpleUserSerializer(read_only=True)
    revertida_em = serializers.DateTimeField(read_only=True, allow_null=True)
    revertida_por = SimpleUserSerializer(read_only=True, allow_null=True)
    motivo_reversao = serializers.CharField(read_only=True, allow_blank=True)
    before_snapshot = serializers.JSONField(read_only=True)
    after_snapshot = serializers.JSONField(read_only=True)
    changes = AdminOverrideChangeReadSerializer(many=True, read_only=True)


class AdminOverrideFinancialFlagsSerializer(serializers.Serializer):
    tem_retorno = serializers.BooleanField()
    tem_baixa_manual = serializers.BooleanField()
    tem_liquidacao = serializers.BooleanField()


class AdminOverrideParcelaEditorSerializer(serializers.Serializer):
    id = serializers.IntegerField(allow_null=True)
    numero = serializers.IntegerField()
    referencia_mes = serializers.DateField()
    valor = serializers.DecimalField(max_digits=10, decimal_places=2)
    data_vencimento = serializers.DateField()
    status = serializers.CharField()
    data_pagamento = serializers.DateField(allow_null=True)
    observacao = serializers.CharField(allow_blank=True)
    layout_bucket = serializers.CharField()
    updated_at = serializers.DateTimeField(allow_null=True)
    financial_flags = AdminOverrideFinancialFlagsSerializer()


class AdminOverrideCicloEditorSerializer(serializers.Serializer):
    id = serializers.IntegerField(allow_null=True)
    numero = serializers.IntegerField()
    data_inicio = serializers.DateField()
    data_fim = serializers.DateField()
    status = serializers.CharField()
    valor_total = serializers.DecimalField(max_digits=10, decimal_places=2)
    updated_at = serializers.DateTimeField(allow_null=True)
    comprovantes_ciclo = ProjectedComprovanteSerializer(many=True, read_only=True)
    termo_antecipacao = ProjectedComprovanteSerializer(read_only=True, allow_null=True)
    parcelas = AdminOverrideParcelaEditorSerializer(many=True)


class AdminOverrideRefinanciamentoEditorSerializer(serializers.Serializer):
    id = serializers.IntegerField(read_only=True)
    status = serializers.CharField(read_only=True)
    competencia_solicitada = serializers.DateField(read_only=True)
    valor_refinanciamento = serializers.DecimalField(
        max_digits=10,
        decimal_places=2,
        read_only=True,
    )
    repasse_agente = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)
    executado_em = serializers.DateTimeField(read_only=True, allow_null=True)
    data_ativacao_ciclo = serializers.DateTimeField(read_only=True, allow_null=True)
    motivo_bloqueio = serializers.CharField(read_only=True, allow_blank=True)
    observacao = serializers.CharField(read_only=True, allow_blank=True)
    analista_note = serializers.CharField(read_only=True, allow_blank=True)
    coordenador_note = serializers.CharField(read_only=True, allow_blank=True)
    reviewed_by_id = serializers.IntegerField(read_only=True, allow_null=True)
    updated_at = serializers.DateTimeField(read_only=True, allow_null=True)


class AdminOverrideContratoEditorSerializer(serializers.Serializer):
    id = serializers.IntegerField(read_only=True)
    updated_at = serializers.DateTimeField(read_only=True, allow_null=True)
    codigo = serializers.CharField(read_only=True)
    status = serializers.CharField(read_only=True)
    valor_bruto = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)
    valor_liquido = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)
    valor_mensalidade = serializers.DecimalField(
        max_digits=10,
        decimal_places=2,
        read_only=True,
    )
    prazo_meses = serializers.IntegerField(read_only=True)
    taxa_antecipacao = serializers.DecimalField(max_digits=5, decimal_places=2, read_only=True)
    margem_disponivel = serializers.DecimalField(
        max_digits=10,
        decimal_places=2,
        read_only=True,
    )
    valor_total_antecipacao = serializers.DecimalField(
        max_digits=10,
        decimal_places=2,
        read_only=True,
    )
    doacao_associado = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)
    comissao_agente = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)
    data_contrato = serializers.DateField(read_only=True, allow_null=True)
    data_aprovacao = serializers.DateField(read_only=True, allow_null=True)
    data_primeira_mensalidade = serializers.DateField(read_only=True, allow_null=True)
    mes_averbacao = serializers.DateField(read_only=True, allow_null=True)
    auxilio_liberado_em = serializers.DateField(read_only=True, allow_null=True)
    ciclo_ja_renovado = serializers.BooleanField(read_only=True)
    contrato_nao_renovado = serializers.BooleanField(read_only=True)
    ciclos = AdminOverrideCicloEditorSerializer(many=True, read_only=True)
    meses_nao_pagos = AdminOverrideParcelaEditorSerializer(many=True, read_only=True)
    movimentos_financeiros_avulsos = AdminOverrideParcelaEditorSerializer(
        many=True,
        read_only=True,
    )
    refinanciamento_ativo = AdminOverrideRefinanciamentoEditorSerializer(
        read_only=True,
        allow_null=True,
    )


class AssociadoAdminSnapshotSerializer(serializers.Serializer):
    id = serializers.IntegerField(read_only=True)
    matricula = serializers.CharField(read_only=True)
    tipo_documento = serializers.CharField(read_only=True)
    nome_completo = serializers.CharField(read_only=True)
    cpf_cnpj = serializers.CharField(read_only=True)
    rg = serializers.CharField(read_only=True, allow_blank=True)
    orgao_expedidor = serializers.CharField(read_only=True, allow_blank=True)
    email = serializers.CharField(read_only=True, allow_blank=True)
    telefone = serializers.CharField(read_only=True, allow_blank=True)
    data_nascimento = serializers.DateField(read_only=True, allow_null=True)
    profissao = serializers.CharField(read_only=True, allow_blank=True)
    estado_civil = serializers.CharField(read_only=True, allow_blank=True)
    orgao_publico = serializers.CharField(read_only=True, allow_blank=True)
    matricula_orgao = serializers.CharField(read_only=True, allow_blank=True)
    cargo = serializers.CharField(read_only=True, allow_blank=True)
    status = serializers.CharField(read_only=True)
    observacao = serializers.CharField(read_only=True, allow_blank=True)
    agente_responsavel_id = serializers.IntegerField(read_only=True, allow_null=True)
    percentual_repasse = serializers.CharField(read_only=True)
    endereco = EnderecoSerializer(read_only=True, allow_null=True)
    dados_bancarios = DadosBancariosSerializer(read_only=True, allow_null=True)
    contato = ContatoHistoricoSerializer(read_only=True, allow_null=True)
    updated_at = serializers.DateTimeField(read_only=True, allow_null=True)


class AdminOverrideDocumentoReadSerializer(serializers.Serializer):
    id = serializers.IntegerField(read_only=True)
    tipo = serializers.CharField(read_only=True)
    status = serializers.CharField(read_only=True)
    observacao = serializers.CharField(read_only=True, allow_blank=True)
    origem = serializers.CharField(read_only=True)
    nome_original = serializers.CharField(read_only=True, allow_blank=True)
    arquivo_referencia = serializers.CharField(read_only=True, allow_blank=True)
    arquivo_disponivel_localmente = serializers.BooleanField(read_only=True)
    tipo_referencia = serializers.CharField(read_only=True, allow_blank=True)
    url = serializers.CharField(read_only=True, allow_blank=True, allow_null=True)
    created_at = serializers.DateTimeField(read_only=True, allow_null=True)
    updated_at = serializers.DateTimeField(read_only=True, allow_null=True)
    deleted_at = serializers.DateTimeField(read_only=True, allow_null=True)


class AdminOverrideEsteiraReadSerializer(serializers.Serializer):
    id = serializers.IntegerField(read_only=True)
    etapa_atual = serializers.CharField(read_only=True)
    status = serializers.CharField(read_only=True)
    prioridade = serializers.IntegerField(read_only=True)
    observacao = serializers.CharField(read_only=True, allow_blank=True)
    analista_responsavel_id = serializers.IntegerField(read_only=True, allow_null=True)
    coordenador_responsavel_id = serializers.IntegerField(read_only=True, allow_null=True)
    tesoureiro_responsavel_id = serializers.IntegerField(read_only=True, allow_null=True)
    concluido_em = serializers.DateTimeField(read_only=True, allow_null=True)
    updated_at = serializers.DateTimeField(read_only=True, allow_null=True)


class AdminOverrideWarningSerializer(serializers.Serializer):
    code = serializers.CharField(read_only=True)
    severity = serializers.CharField(read_only=True)
    contrato_id = serializers.IntegerField(read_only=True, allow_null=True)
    contrato_codigo = serializers.CharField(read_only=True, allow_blank=True)
    scope = serializers.CharField(read_only=True, allow_blank=True)
    competencia = serializers.DateField(read_only=True, allow_null=True)
    action = serializers.CharField(read_only=True, allow_blank=True)
    message = serializers.CharField(read_only=True)
    details = serializers.JSONField(read_only=True)


class AdminOverrideInactivationReversalSerializer(serializers.Serializer):
    event_id = serializers.IntegerField(read_only=True, allow_null=True)
    available = serializers.BooleanField(read_only=True)
    previous_status = serializers.CharField(read_only=True, allow_blank=True)
    target_status = serializers.CharField(read_only=True, allow_blank=True)
    event_created_at = serializers.DateTimeField(read_only=True, allow_null=True)
    realizado_por = SimpleUserSerializer(read_only=True, allow_null=True)


class AdminOverrideLegacyInactivationReversalSerializer(serializers.Serializer):
    available = serializers.BooleanField(read_only=True)
    current_status = serializers.CharField(read_only=True, allow_blank=True)
    suggested_status = serializers.CharField(read_only=True, allow_blank=True)
    suggested_esteira_etapa = serializers.CharField(read_only=True, allow_blank=True)
    suggested_esteira_status = serializers.CharField(read_only=True, allow_blank=True)


class AdminOverrideEditorPayloadSerializer(serializers.Serializer):
    associado = AssociadoAdminSnapshotSerializer(read_only=True)
    contratos = AdminOverrideContratoEditorSerializer(many=True, read_only=True)
    esteira = AdminOverrideEsteiraReadSerializer(read_only=True, allow_null=True)
    documentos = AdminOverrideDocumentoReadSerializer(many=True, read_only=True)
    inactivation_reversal = AdminOverrideInactivationReversalSerializer(
        read_only=True,
        allow_null=True,
    )
    legacy_inactivation_reversal = (
        AdminOverrideLegacyInactivationReversalSerializer(
            read_only=True,
            allow_null=True,
        )
    )
    warnings = AdminOverrideWarningSerializer(many=True, read_only=True)


class EnderecoAdminWriteSerializer(serializers.Serializer):
    cep = serializers.CharField(required=False, allow_blank=True)
    logradouro = serializers.CharField(required=False, allow_blank=True)
    endereco = serializers.CharField(required=False, allow_blank=True)
    numero = serializers.CharField(required=False, allow_blank=True)
    complemento = serializers.CharField(required=False, allow_blank=True)
    bairro = serializers.CharField(required=False, allow_blank=True)
    cidade = serializers.CharField(required=False, allow_blank=True)
    uf = serializers.CharField(required=False, allow_blank=True)


class DadosBancariosAdminWriteSerializer(serializers.Serializer):
    banco = serializers.CharField(required=False, allow_blank=True)
    agencia = serializers.CharField(required=False, allow_blank=True)
    conta = serializers.CharField(required=False, allow_blank=True)
    tipo_conta = serializers.CharField(required=False, allow_blank=True)
    chave_pix = serializers.CharField(required=False, allow_blank=True)


class ContatoAdminWriteSerializer(serializers.Serializer):
    celular = serializers.CharField(required=False, allow_blank=True)
    email = serializers.CharField(required=False, allow_blank=True)
    orgao_publico = serializers.CharField(required=False, allow_blank=True)
    situacao_servidor = serializers.CharField(required=False, allow_blank=True)
    matricula_servidor = serializers.CharField(required=False, allow_blank=True)


class AssociadoCoreOverrideWriteSerializer(serializers.Serializer):
    updated_at = serializers.DateTimeField(required=False, allow_null=True)
    contrato_updated_at = serializers.DateTimeField(required=False, allow_null=True)
    motivo = serializers.CharField()
    nome_completo = serializers.CharField(required=False, allow_blank=True)
    rg = serializers.CharField(required=False, allow_blank=True)
    orgao_expedidor = serializers.CharField(required=False, allow_blank=True)
    profissao = serializers.CharField(required=False, allow_blank=True)
    estado_civil = serializers.CharField(required=False, allow_blank=True)
    cargo = serializers.CharField(required=False, allow_blank=True)
    status = serializers.CharField(required=False, allow_blank=True)
    observacao = serializers.CharField(required=False, allow_blank=True)
    data_nascimento = serializers.DateField(required=False, allow_null=True)
    agente_responsavel_id = serializers.IntegerField(required=False, allow_null=True)
    percentual_repasse = serializers.DecimalField(
        max_digits=6,
        decimal_places=2,
        required=False,
    )
    endereco = EnderecoAdminWriteSerializer(required=False)
    dados_bancarios = DadosBancariosAdminWriteSerializer(required=False)
    contato = ContatoAdminWriteSerializer(required=False)
    valor_bruto_total = serializers.DecimalField(
        max_digits=10,
        decimal_places=2,
        required=False,
    )
    valor_liquido = serializers.DecimalField(max_digits=10, decimal_places=2, required=False)
    prazo_meses = serializers.IntegerField(required=False, min_value=3, max_value=4)
    taxa_antecipacao = serializers.DecimalField(
        max_digits=5,
        decimal_places=2,
        required=False,
    )
    mensalidade = serializers.DecimalField(max_digits=10, decimal_places=2, required=False)
    margem_disponivel = serializers.DecimalField(
        max_digits=10,
        decimal_places=2,
        required=False,
    )
    status_contrato = serializers.CharField(required=False, allow_blank=True)


class ContratoCoreOverrideWriteSerializer(serializers.Serializer):
    updated_at = serializers.DateTimeField(required=False, allow_null=True)
    motivo = serializers.CharField()
    status = serializers.CharField(required=False, allow_blank=True)
    contato_web = serializers.BooleanField(required=False)
    termos_web = serializers.BooleanField(required=False)
    agente_id = serializers.IntegerField(required=False, allow_null=True)
    prazo_meses = serializers.IntegerField(required=False, min_value=3, max_value=4)
    valor_bruto = serializers.DecimalField(max_digits=10, decimal_places=2, required=False)
    valor_liquido = serializers.DecimalField(max_digits=10, decimal_places=2, required=False)
    valor_mensalidade = serializers.DecimalField(
        max_digits=10,
        decimal_places=2,
        required=False,
    )
    taxa_antecipacao = serializers.DecimalField(
        max_digits=5,
        decimal_places=2,
        required=False,
    )
    margem_disponivel = serializers.DecimalField(
        max_digits=10,
        decimal_places=2,
        required=False,
    )
    valor_total_antecipacao = serializers.DecimalField(
        max_digits=10,
        decimal_places=2,
        required=False,
    )
    doacao_associado = serializers.DecimalField(
        max_digits=10,
        decimal_places=2,
        required=False,
    )
    comissao_agente = serializers.DecimalField(
        max_digits=10,
        decimal_places=2,
        required=False,
    )
    data_contrato = serializers.DateField(required=False, allow_null=True)
    data_aprovacao = serializers.DateField(required=False, allow_null=True)
    data_primeira_mensalidade = serializers.DateField(required=False, allow_null=True)
    mes_averbacao = serializers.DateField(required=False, allow_null=True)
    auxilio_liberado_em = serializers.DateField(required=False, allow_null=True)


class RefinanciamentoOverrideWriteSerializer(serializers.Serializer):
    updated_at = serializers.DateTimeField(required=False, allow_null=True)
    motivo = serializers.CharField()
    status = serializers.CharField(required=False, allow_blank=True)
    competencia_solicitada = serializers.DateField(required=False, allow_null=True)
    valor_refinanciamento = serializers.DecimalField(
        max_digits=10,
        decimal_places=2,
        required=False,
    )
    repasse_agente = serializers.DecimalField(max_digits=10, decimal_places=2, required=False)
    executado_em = serializers.DateTimeField(required=False, allow_null=True)
    data_ativacao_ciclo = serializers.DateTimeField(required=False, allow_null=True)
    motivo_bloqueio = serializers.CharField(required=False, allow_blank=True)
    observacao = serializers.CharField(required=False, allow_blank=True)
    analista_note = serializers.CharField(required=False, allow_blank=True)
    coordenador_note = serializers.CharField(required=False, allow_blank=True)
    reviewed_by_id = serializers.IntegerField(required=False, allow_null=True)


class LegacyInactivationReversalWriteSerializer(serializers.Serializer):
    motivo = serializers.CharField()
    status_retorno = serializers.ChoiceField(
        choices=[
            "cadastrado",
            "importado",
            "em_analise",
            "ativo",
            "pendente",
            "apto_a_renovar",
        ]
    )
    etapa_esteira = serializers.ChoiceField(
        choices=[
            "cadastro",
            "analise",
            "coordenacao",
            "tesouraria",
        ]
    )
    status_esteira = serializers.ChoiceField(
        choices=[
            "aguardando",
            "em_andamento",
            "pendenciado",
            "aprovado",
        ]
    )
    observacao_esteira = serializers.CharField(required=False, allow_blank=True)


class EsteiraOverrideWriteSerializer(serializers.Serializer):
    updated_at = serializers.DateTimeField(required=False, allow_null=True)
    motivo = serializers.CharField()
    etapa_atual = serializers.CharField(required=False, allow_blank=True)
    status = serializers.CharField(required=False, allow_blank=True)
    prioridade = serializers.IntegerField(required=False)
    observacao = serializers.CharField(required=False, allow_blank=True)


class RenewalStageTransitionWriteSerializer(serializers.Serializer):
    contrato_id = serializers.IntegerField()
    motivo = serializers.CharField()
    target_stage = serializers.CharField()


class SaveAllContratoCoreWriteSerializer(serializers.Serializer):
    updated_at = serializers.DateTimeField(required=False, allow_null=True)
    status = serializers.CharField(required=False, allow_blank=True)
    contato_web = serializers.BooleanField(required=False)
    termos_web = serializers.BooleanField(required=False)
    agente_id = serializers.IntegerField(required=False, allow_null=True)
    prazo_meses = serializers.IntegerField(required=False, min_value=3, max_value=4)
    valor_bruto = serializers.DecimalField(max_digits=10, decimal_places=2, required=False)
    valor_liquido = serializers.DecimalField(max_digits=10, decimal_places=2, required=False)
    valor_mensalidade = serializers.DecimalField(
        max_digits=10,
        decimal_places=2,
        required=False,
    )
    taxa_antecipacao = serializers.DecimalField(
        max_digits=5,
        decimal_places=2,
        required=False,
    )
    margem_disponivel = serializers.DecimalField(
        max_digits=10,
        decimal_places=2,
        required=False,
    )
    valor_total_antecipacao = serializers.DecimalField(
        max_digits=10,
        decimal_places=2,
        required=False,
    )
    doacao_associado = serializers.DecimalField(
        max_digits=10,
        decimal_places=2,
        required=False,
    )
    comissao_agente = serializers.DecimalField(
        max_digits=10,
        decimal_places=2,
        required=False,
    )
    data_contrato = serializers.DateField(required=False, allow_null=True)
    data_aprovacao = serializers.DateField(required=False, allow_null=True)
    data_primeira_mensalidade = serializers.DateField(required=False, allow_null=True)
    mes_averbacao = serializers.DateField(required=False, allow_null=True)
    auxilio_liberado_em = serializers.DateField(required=False, allow_null=True)


class SaveAllRefinanciamentoWriteSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    updated_at = serializers.DateTimeField(required=False, allow_null=True)
    status = serializers.CharField(required=False, allow_blank=True)
    competencia_solicitada = serializers.DateField(required=False, allow_null=True)
    valor_refinanciamento = serializers.DecimalField(
        max_digits=10,
        decimal_places=2,
        required=False,
    )
    repasse_agente = serializers.DecimalField(max_digits=10, decimal_places=2, required=False)
    executado_em = serializers.DateTimeField(required=False, allow_null=True)
    data_ativacao_ciclo = serializers.DateTimeField(required=False, allow_null=True)
    motivo_bloqueio = serializers.CharField(required=False, allow_blank=True)
    observacao = serializers.CharField(required=False, allow_blank=True)
    analista_note = serializers.CharField(required=False, allow_blank=True)
    coordenador_note = serializers.CharField(required=False, allow_blank=True)
    reviewed_by_id = serializers.IntegerField(required=False, allow_null=True)


class SaveAllEsteiraWriteSerializer(serializers.Serializer):
    updated_at = serializers.DateTimeField(required=False, allow_null=True)
    etapa_atual = serializers.CharField(required=False, allow_blank=True)
    status = serializers.CharField(required=False, allow_blank=True)
    prioridade = serializers.IntegerField(required=False)
    observacao = serializers.CharField(required=False, allow_blank=True)


class ParcelaLayoutWriteSerializer(serializers.Serializer):
    id = serializers.IntegerField(required=False, allow_null=True)
    cycle_ref = serializers.CharField(required=False, allow_blank=True)
    cycle_id = serializers.IntegerField(required=False, allow_null=True)
    numero = serializers.IntegerField()
    referencia_mes = serializers.DateField()
    valor = serializers.DecimalField(max_digits=10, decimal_places=2)
    data_vencimento = serializers.DateField()
    status = serializers.CharField()
    data_pagamento = serializers.DateField(required=False, allow_null=True)
    observacao = serializers.CharField(required=False, allow_blank=True)
    layout_bucket = serializers.CharField(required=False, allow_blank=True)


class CicloLayoutWriteSerializer(serializers.Serializer):
    id = serializers.IntegerField(required=False, allow_null=True)
    client_key = serializers.CharField(required=False, allow_blank=True)
    numero = serializers.IntegerField()
    data_inicio = serializers.DateField()
    data_fim = serializers.DateField()
    status = serializers.CharField()
    valor_total = serializers.DecimalField(max_digits=10, decimal_places=2)


class CycleLayoutOverrideWriteSerializer(serializers.Serializer):
    updated_at = serializers.DateTimeField(required=False, allow_null=True)
    motivo = serializers.CharField()
    cycles = CicloLayoutWriteSerializer(many=True)
    parcelas = ParcelaLayoutWriteSerializer(many=True)


class SaveAllCycleLayoutWriteSerializer(serializers.Serializer):
    updated_at = serializers.DateTimeField(required=False, allow_null=True)
    cycles = CicloLayoutWriteSerializer(many=True)
    parcelas = ParcelaLayoutWriteSerializer(many=True)


class SaveAllContratoWriteSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    dirty_sections = serializers.ListField(
        child=serializers.ChoiceField(
            choices=["contract_core", "cycle_layout", "refinanciamento"]
        ),
        required=False,
        allow_empty=True,
    )
    core = SaveAllContratoCoreWriteSerializer(required=False)
    cycles = SaveAllCycleLayoutWriteSerializer(required=False)
    refinanciamento = SaveAllRefinanciamentoWriteSerializer(required=False)


class AdminOverrideSaveAllWriteSerializer(serializers.Serializer):
    motivo = serializers.CharField()
    dirty_sections = serializers.ListField(
        child=serializers.ChoiceField(choices=["esteira"]),
        required=False,
        allow_empty=True,
    )
    contratos = SaveAllContratoWriteSerializer(many=True, required=False)
    esteira = SaveAllEsteiraWriteSerializer(required=False)


class DocumentoVersionWriteSerializer(serializers.Serializer):
    updated_at = serializers.DateTimeField(required=False, allow_null=True)
    motivo = serializers.CharField()
    arquivo = serializers.FileField(required=False, allow_null=True)
    status = serializers.CharField(required=False, allow_blank=True)
    observacao = serializers.CharField(required=False, allow_blank=True)


class ComprovanteVersionWriteSerializer(serializers.Serializer):
    updated_at = serializers.DateTimeField(required=False, allow_null=True)
    motivo = serializers.CharField()
    arquivo = serializers.FileField(required=False, allow_null=True)
    tipo = serializers.CharField(required=False, allow_blank=True)
    papel = serializers.CharField(required=False, allow_blank=True)
    origem = serializers.CharField(required=False, allow_blank=True)
    status_validacao = serializers.CharField(required=False, allow_blank=True)
    data_pagamento = serializers.DateTimeField(required=False, allow_null=True)


class ComprovanteCreateWriteSerializer(serializers.Serializer):
    ciclo_id = serializers.IntegerField()
    motivo = serializers.CharField()
    tipo = serializers.CharField(required=False, allow_blank=True)
    papel = serializers.CharField(required=False, allow_blank=True)
    origem = serializers.CharField(required=False, allow_blank=True)
    status_validacao = serializers.CharField(required=False, allow_blank=True)
    data_pagamento = serializers.DateTimeField(required=False, allow_null=True)


class AdminOverrideReverterWriteSerializer(serializers.Serializer):
    motivo_reversao = serializers.CharField()
