from __future__ import annotations

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from .financeiro import build_financeiro_payload, build_financeiro_resumo
from .models import ArquivoRetorno, ArquivoRetornoItem, DuplicidadeFinanceira


class ArquivoRetornoResumoSerializer(serializers.Serializer):
    competencia = serializers.CharField(required=False, allow_blank=True)
    data_geracao = serializers.CharField(required=False, allow_blank=True)
    entidade = serializers.CharField(required=False, allow_blank=True)
    sistema_origem = serializers.CharField(required=False, allow_blank=True)
    baixa_efetuada = serializers.IntegerField(required=False)
    nao_descontado = serializers.IntegerField(required=False)
    pendencias_manuais = serializers.IntegerField(required=False)
    duplicidades = serializers.IntegerField(required=False)
    nao_encontrado = serializers.IntegerField(required=False)
    associados_importados = serializers.IntegerField(required=False)
    erro = serializers.IntegerField(required=False)
    ciclo_aberto = serializers.IntegerField(required=False)
    encerramentos = serializers.IntegerField(required=False)
    novos_ciclos = serializers.IntegerField(required=False)
    efetivados = serializers.IntegerField(required=False)
    nao_descontados = serializers.IntegerField(required=False)
    cpfs_duplicados_arquivo = serializers.IntegerField(required=False)
    linhas_duplicadas_ignoradas = serializers.IntegerField(required=False)
    pm_cpfs_duplicados_arquivo = serializers.IntegerField(required=False)
    pm_linhas_duplicadas_ignoradas = serializers.IntegerField(required=False)


class ArquivoRetornoFinanceiroGrupoSerializer(serializers.Serializer):
    esperado = serializers.DecimalField(max_digits=12, decimal_places=2)
    recebido = serializers.DecimalField(max_digits=12, decimal_places=2)
    ok = serializers.IntegerField()
    total = serializers.IntegerField()
    faltando = serializers.IntegerField()
    pendente = serializers.DecimalField(max_digits=12, decimal_places=2)
    percentual = serializers.FloatField()


class ArquivoRetornoFinanceiroResumoSerializer(ArquivoRetornoFinanceiroGrupoSerializer):
    mensalidades = ArquivoRetornoFinanceiroGrupoSerializer()
    valores_30_50 = ArquivoRetornoFinanceiroGrupoSerializer()


class ArquivoRetornoFinanceiroItemSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    associado_id = serializers.IntegerField(required=False, allow_null=True)
    associado_nome = serializers.CharField()
    agente_responsavel = serializers.CharField(required=False, allow_blank=True)
    matricula = serializers.CharField(required=False, allow_blank=True)
    cpf_cnpj = serializers.CharField()
    valor = serializers.DecimalField(max_digits=12, decimal_places=2)
    esperado = serializers.DecimalField(max_digits=12, decimal_places=2)
    recebido = serializers.DecimalField(max_digits=12, decimal_places=2)
    status_code = serializers.CharField(required=False, allow_blank=True)
    status_label = serializers.CharField(required=False, allow_blank=True)
    ok = serializers.BooleanField()
    situacao_code = serializers.CharField()
    situacao_label = serializers.CharField()
    orgao_pagto = serializers.CharField(required=False, allow_blank=True)
    relatorio = serializers.CharField(required=False, allow_blank=True)
    manual_status = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    manual_valor = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False, allow_null=True
    )
    manual_forma_pagamento = serializers.CharField(
        required=False, allow_null=True, allow_blank=True
    )
    manual_paid_at = serializers.DateTimeField(required=False, allow_null=True)
    manual_comprovante_path = serializers.CharField(
        required=False, allow_null=True, allow_blank=True
    )
    origem_baixa = serializers.CharField(required=False, allow_blank=True)
    arquivo_referencia = serializers.CharField(
        required=False, allow_null=True, allow_blank=True
    )
    arquivo_disponivel_localmente = serializers.BooleanField(required=False)
    tipo_referencia = serializers.CharField(required=False, allow_blank=True)
    categoria = serializers.CharField()


class ArquivoRetornoFinanceiroPayloadSerializer(serializers.Serializer):
    resumo = ArquivoRetornoFinanceiroResumoSerializer()
    rows = ArquivoRetornoFinanceiroItemSerializer(many=True)


class ArquivoRetornoUploadSerializer(serializers.Serializer):
    arquivo = serializers.FileField()


class DuplicidadeFinanceiraItemSerializer(serializers.Serializer):
    id = serializers.IntegerField(read_only=True)
    arquivo_retorno_item_id = serializers.IntegerField(read_only=True)
    arquivo_retorno_id = serializers.IntegerField(read_only=True)
    arquivo_nome = serializers.CharField(read_only=True)
    linha_numero = serializers.IntegerField(read_only=True)
    associado_id = serializers.IntegerField(read_only=True, allow_null=True)
    nome = serializers.CharField(read_only=True)
    cpf_cnpj = serializers.CharField(read_only=True)
    matricula = serializers.CharField(read_only=True, allow_blank=True)
    agente_nome = serializers.CharField(read_only=True, allow_blank=True)
    contrato_id = serializers.IntegerField(read_only=True, allow_null=True)
    contrato_codigo = serializers.CharField(read_only=True, allow_blank=True)
    motivo = serializers.ChoiceField(
        choices=DuplicidadeFinanceira.Motivo.choices,
        read_only=True,
    )
    status = serializers.ChoiceField(
        choices=DuplicidadeFinanceira.Status.choices,
        read_only=True,
    )
    competencia_retorno = serializers.DateField(read_only=True)
    competencia_manual = serializers.DateField(read_only=True, allow_null=True)
    valor_retorno = serializers.DecimalField(
        max_digits=12,
        decimal_places=2,
        read_only=True,
        allow_null=True,
    )
    valor_manual = serializers.DecimalField(
        max_digits=12,
        decimal_places=2,
        read_only=True,
        allow_null=True,
    )
    observacao = serializers.CharField(read_only=True, allow_blank=True)
    devolucao_id = serializers.IntegerField(read_only=True, allow_null=True)
    resolvido_em = serializers.DateTimeField(read_only=True, allow_null=True)
    resolvido_por = serializers.CharField(read_only=True, allow_blank=True)
    motivo_resolucao = serializers.CharField(read_only=True, allow_blank=True)
    created_at = serializers.DateTimeField(read_only=True)


class DuplicidadeFinanceiraKpisSerializer(serializers.Serializer):
    total = serializers.IntegerField(read_only=True)
    abertas = serializers.IntegerField(read_only=True)
    em_tratamento = serializers.IntegerField(read_only=True)
    resolvidas = serializers.IntegerField(read_only=True)
    descartadas = serializers.IntegerField(read_only=True)


class DuplicidadeFinanceiraResolverSerializer(serializers.Serializer):
    data_devolucao = serializers.DateField(required=True)
    valor = serializers.DecimalField(max_digits=12, decimal_places=2, required=True)
    motivo = serializers.CharField(required=True, allow_blank=False)

    def validate(self, attrs):
        request = self.context.get("request")
        files = []
        if request is not None:
            files.extend(request.FILES.getlist("comprovantes"))
            single = request.FILES.get("comprovante")
            if single is not None and single not in files:
                files.append(single)
        if not files:
            raise serializers.ValidationError(
                {"comprovantes": "Envie pelo menos um comprovante."}
            )
        attrs["comprovantes"] = files
        return attrs


class DuplicidadeFinanceiraDescartarSerializer(serializers.Serializer):
    motivo = serializers.CharField(required=True, allow_blank=False)


class ArquivoRetornoItemSerializer(serializers.ModelSerializer):
    associado_nome = serializers.CharField(source="associado.nome_completo", read_only=True)
    contrato_codigo = serializers.CharField(source="parcela.ciclo.contrato.codigo", read_only=True)
    associado_id = serializers.IntegerField(read_only=True)
    associado_matricula = serializers.SerializerMethodField()
    agente_responsavel = serializers.SerializerMethodField()

    def get_associado_matricula(self, obj: ArquivoRetornoItem) -> str:
        associado = obj.associado
        if associado:
            return associado.matricula_orgao or associado.matricula or obj.matricula_servidor
        return obj.matricula_servidor

    def get_agente_responsavel(self, obj: ArquivoRetornoItem) -> str:
        associado = obj.associado
        if associado and associado.agente_responsavel:
            return associado.agente_responsavel.full_name

        contrato = getattr(getattr(getattr(obj.parcela, "ciclo", None), "contrato", None), "agente", None)
        return contrato.full_name if contrato else ""

    class Meta:
        model = ArquivoRetornoItem
        fields = [
            "id",
            "linha_numero",
            "cpf_cnpj",
            "matricula_servidor",
            "nome_servidor",
            "cargo",
            "competencia",
            "valor_descontado",
            "status_codigo",
            "status_desconto",
            "status_descricao",
            "motivo_rejeicao",
            "orgao_codigo",
            "orgao_pagto_codigo",
            "orgao_pagto_nome",
            "resultado_processamento",
            "observacao",
            "gerou_encerramento",
            "gerou_novo_ciclo",
            "associado_id",
            "associado_nome",
            "associado_matricula",
            "agente_responsavel",
            "contrato_codigo",
        ]


class ArquivoRetornoListSerializer(serializers.ModelSerializer):
    competencia_display = serializers.SerializerMethodField()
    sistema_origem = serializers.CharField(source="orgao_origem", read_only=True)
    uploaded_by_nome = serializers.CharField(source="uploaded_by.full_name", read_only=True)
    associados_importados = serializers.SerializerMethodField()
    resumo = serializers.SerializerMethodField()
    financeiro = serializers.SerializerMethodField()

    class Meta:
        model = ArquivoRetorno
        fields = [
            "id",
            "arquivo_nome",
            "formato",
            "sistema_origem",
            "competencia",
            "competencia_display",
            "total_registros",
            "processados",
            "associados_importados",
            "nao_encontrados",
            "erros",
            "status",
            "resumo",
            "financeiro",
            "uploaded_by_nome",
            "created_at",
            "processado_em",
        ]

    def get_competencia_display(self, obj: ArquivoRetorno) -> str:
        return obj.competencia.strftime("%m/%Y")

    def get_resumo(self, obj: ArquivoRetorno) -> dict:
        view = self.context.get("view")
        if getattr(view, "action", None) == "list":
            return {}
        return ArquivoRetornoResumoSerializer(obj.resultado_resumo).data

    def get_associados_importados(self, obj: ArquivoRetorno) -> int:
        if isinstance(obj.resultado_resumo, dict):
            return int(obj.resultado_resumo.get("associados_importados") or 0)
        return 0

    def get_financeiro(self, obj: ArquivoRetorno) -> dict:
        cached = None
        if isinstance(obj.resultado_resumo, dict):
            maybe_cached = obj.resultado_resumo.get("financeiro")
            if isinstance(maybe_cached, dict):
                cached = maybe_cached

        view = self.context.get("view")
        action = getattr(view, "action", None)

        # A listagem/histórico da importação entra em polling frequente. Nessa rota,
        # o resumo financeiro não é consumido pela UI e não deve recalcular nem
        # serializar payloads pesados do domínio financeiro.
        if action == "list":
            return None

        # Durante o polling da importação, recalcular o resumo financeiro completo
        # a cada refresh aumenta muito a carga do backend e pode derrubar o upstream
        # em VPS menores. Enquanto o arquivo não estiver concluído, a UI já mostra
        # progresso pelo próprio ArquivoRetorno; o financeiro detalhado só precisa
        # ser calculado após a conclusão.
        if obj.status != ArquivoRetorno.Status.CONCLUIDO:
            return None

        if cached is not None:
            return ArquivoRetornoFinanceiroResumoSerializer(cached).data

        cache = self.context.setdefault("_financeiro_cache", {})
        if obj.competencia not in cache:
            cache[obj.competencia] = build_financeiro_resumo(competencia=obj.competencia)
        return ArquivoRetornoFinanceiroResumoSerializer(cache[obj.competencia]).data


# ---------------------------------------------------------------------------
# Dry-run serializers
# ---------------------------------------------------------------------------

class DryRunValores3050GrupoSerializer(serializers.Serializer):
    count = serializers.IntegerField()
    valor_total = serializers.DecimalField(max_digits=12, decimal_places=2)


class DryRunValores3050Serializer(serializers.Serializer):
    descontaram = DryRunValores3050GrupoSerializer()
    nao_descontaram = DryRunValores3050GrupoSerializer()


class DryRunMudancaStatusSerializer(serializers.Serializer):
    antes = serializers.CharField()
    depois = serializers.CharField()
    count = serializers.IntegerField()


class DryRunKpisSerializer(serializers.Serializer):
    total_no_arquivo = serializers.IntegerField()
    atualizados = serializers.IntegerField()
    baixa_efetuada = serializers.IntegerField()
    nao_descontado = serializers.IntegerField()
    nao_encontrado = serializers.IntegerField()
    associados_importados = serializers.IntegerField()
    pendencia_manual = serializers.IntegerField()
    ciclo_aberto = serializers.IntegerField()
    valor_previsto = serializers.DecimalField(max_digits=14, decimal_places=2)
    valor_real = serializers.DecimalField(max_digits=14, decimal_places=2)
    aptos_a_renovar = serializers.IntegerField()
    associados_inativos_com_desconto = serializers.IntegerField(default=0)
    valores_30_50 = DryRunValores3050Serializer()
    mudancas_status_associado = DryRunMudancaStatusSerializer(many=True)
    mudancas_status_ciclo = DryRunMudancaStatusSerializer(many=True)


class DryRunItemSerializer(serializers.Serializer):
    linha_numero = serializers.IntegerField(allow_null=True)
    cpf_cnpj = serializers.CharField()
    nome_servidor = serializers.CharField(allow_blank=True)
    matricula_servidor = serializers.CharField(allow_blank=True)
    orgao_pagto_nome = serializers.CharField(allow_blank=True)
    valor_descontado = serializers.DecimalField(max_digits=10, decimal_places=2)
    status_codigo = serializers.CharField(allow_blank=True)
    resultado = serializers.CharField()
    associado_id = serializers.IntegerField(allow_null=True)
    associado_nome = serializers.CharField(allow_blank=True)
    associado_status_antes = serializers.CharField(allow_null=True)
    associado_status_depois = serializers.CharField(allow_null=True)
    ciclo_status_antes = serializers.CharField(allow_null=True)
    ciclo_status_depois = serializers.CharField(allow_null=True)
    ficara_apto_renovar = serializers.BooleanField()
    desconto_em_associado_inativo = serializers.BooleanField()
    categoria = serializers.CharField()


class DryRunResultadoSerializer(serializers.Serializer):
    kpis = DryRunKpisSerializer()
    items = DryRunItemSerializer(many=True)


class ArquivoRetornoDetailSerializer(ArquivoRetornoListSerializer):
    dry_run_resultado = serializers.SerializerMethodField()

    class Meta(ArquivoRetornoListSerializer.Meta):
        fields = ArquivoRetornoListSerializer.Meta.fields + ["arquivo_url", "dry_run_resultado"]

    @extend_schema_field(DryRunResultadoSerializer(allow_null=True))
    def get_dry_run_resultado(self, obj: ArquivoRetorno):
        if obj.status != ArquivoRetorno.Status.AGUARDANDO_CONFIRMACAO:
            return None
        if not obj.dry_run_resultado:
            return None
        return DryRunResultadoSerializer(obj.dry_run_resultado).data
