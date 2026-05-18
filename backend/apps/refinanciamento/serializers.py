from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from apps.associados.serializers import SimpleUserSerializer
from apps.contratos.cycle_timeline import (
    get_contract_first_cycle_activation_info,
    get_contract_cycle_size,
    get_refinanciamento_activation_info,
)
from core.file_references import build_filefield_reference

from .models import Comprovante, Refinanciamento
from .treasury_value_repair import _resolve_contrato


class ComprovanteResumoSerializer(serializers.ModelSerializer):
    enviado_por = SimpleUserSerializer(read_only=True)
    arquivo_referencia = serializers.SerializerMethodField()
    arquivo_disponivel_localmente = serializers.SerializerMethodField()
    tipo_referencia = serializers.SerializerMethodField()

    class Meta:
        model = Comprovante
        fields = [
            "id",
            "refinanciamento",
            "contrato",
            "ciclo",
            "tipo",
            "papel",
            "arquivo",
            "arquivo_referencia",
            "arquivo_disponivel_localmente",
            "tipo_referencia",
            "nome_original",
            "mime",
            "size_bytes",
            "data_pagamento",
            "origem",
            "legacy_comprovante_id",
            "enviado_por",
            "created_at",
        ]

    def get_arquivo_referencia(self, obj: Comprovante) -> str:
        return self._reference(obj).arquivo_referencia

    def get_arquivo_disponivel_localmente(self, obj: Comprovante) -> bool:
        return self._reference(obj).arquivo_disponivel_localmente

    def get_tipo_referencia(self, obj: Comprovante) -> str:
        return self._reference(obj).tipo_referencia

    def _reference(self, obj: Comprovante):
        request = self.context.get("request")
        missing_type = (
            "legado_sem_arquivo"
            if obj.origem == Comprovante.Origem.LEGADO
            else "referencia_sem_arquivo"
        )
        reference = build_filefield_reference(
            obj.arquivo,
            request=request,
            missing_type=missing_type,
        )
        if obj.arquivo_referencia and obj.arquivo_referencia != reference.arquivo_referencia:
            return reference.__class__(
                url=reference.url,
                arquivo_referencia=obj.arquivo_referencia,
                arquivo_disponivel_localmente=reference.arquivo_disponivel_localmente,
                tipo_referencia=reference.tipo_referencia,
            )
        return reference


class RefinanciamentoItemResumoSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    numero = serializers.IntegerField()
    pagamento_mensalidade_id = serializers.IntegerField(allow_null=True)
    referencia_mes = serializers.DateField()
    valor = serializers.DecimalField(max_digits=12, decimal_places=2)
    status = serializers.CharField()


class RefinanciamentoAuditoriaSerializer(serializers.Serializer):
    solicitado_por = SimpleUserSerializer(allow_null=True)
    aprovado_por = SimpleUserSerializer(allow_null=True)
    bloqueado_por = SimpleUserSerializer(allow_null=True)
    efetivado_por = SimpleUserSerializer(allow_null=True)
    reviewed_by = SimpleUserSerializer(allow_null=True)
    reviewed_at = serializers.DateTimeField(allow_null=True)
    analista_note = serializers.CharField(allow_null=True, allow_blank=True)
    coordenador_note = serializers.CharField(allow_null=True, allow_blank=True)
    observacao = serializers.CharField(allow_null=True, allow_blank=True)
    motivo_bloqueio = serializers.CharField(allow_null=True, allow_blank=True)


class RefinanciamentoListSerializer(serializers.ModelSerializer):
    contrato_id = serializers.IntegerField(source="contrato_origem.id", read_only=True)
    contrato_codigo = serializers.CharField(
        source="contrato_origem.codigo", read_only=True
    )
    associado_id = serializers.IntegerField(source="associado.id", read_only=True)
    associado_nome = serializers.CharField(
        source="associado.nome_completo", read_only=True
    )
    cpf_cnpj = serializers.CharField(source="associado.cpf_cnpj", read_only=True)
    matricula = serializers.CharField(source="associado.matricula", read_only=True)
    matricula_display = serializers.SerializerMethodField()
    agente = SimpleUserSerializer(source="contrato_origem.agente", read_only=True)
    solicitado_por = SimpleUserSerializer(read_only=True)
    aprovado_por = SimpleUserSerializer(read_only=True)
    bloqueado_por = SimpleUserSerializer(read_only=True)
    efetivado_por = SimpleUserSerializer(read_only=True)
    reviewed_by = SimpleUserSerializer(read_only=True)
    comprovantes = serializers.SerializerMethodField()
    data_anexo_associado = serializers.SerializerMethodField()
    data_anexo_agente = serializers.SerializerMethodField()
    data_pagamento_associado = serializers.SerializerMethodField()
    data_pagamento_agente = serializers.SerializerMethodField()
    ciclo_key = serializers.SerializerMethodField()
    referencias = serializers.SerializerMethodField()
    itens = serializers.SerializerMethodField()
    mensalidades_pagas = serializers.SerializerMethodField()
    mensalidades_total = serializers.SerializerMethodField()
    numero_ciclos = serializers.SerializerMethodField()
    refinanciamento_numero = serializers.SerializerMethodField()
    auditoria = serializers.SerializerMethodField()
    pagamento_status = serializers.SerializerMethodField()
    data_primeiro_ciclo_ativado = serializers.SerializerMethodField()
    data_ativacao_ciclo = serializers.SerializerMethodField()
    origem_data_ativacao = serializers.SerializerMethodField()
    data_solicitacao_renovacao = serializers.SerializerMethodField()
    data_solicitacao = serializers.DateTimeField(source="created_at", read_only=True)
    ativacao_inferida = serializers.SerializerMethodField()
    etapa_operacional = serializers.SerializerMethodField()
    data_renovacao = serializers.SerializerMethodField()
    origem_renovacao = serializers.SerializerMethodField()
    motivo_apto_renovacao = serializers.SerializerMethodField()
    valor_liberado_associado = serializers.SerializerMethodField()
    repasse_agente = serializers.SerializerMethodField()
    legacy_refinanciamento_id = serializers.IntegerField(read_only=True)
    origem = serializers.CharField(read_only=True)

    class Meta:
        model = Refinanciamento
        fields = [
            "id",
            "contrato_id",
            "contrato_codigo",
            "associado_id",
            "associado_nome",
            "cpf_cnpj",
            "matricula",
            "matricula_display",
            "agente",
            "solicitado_por",
            "aprovado_por",
            "bloqueado_por",
            "efetivado_por",
            "reviewed_by",
            "competencia_solicitada",
            "status",
            "valor_refinanciamento",
            "valor_liberado_associado",
            "repasse_agente",
            "ciclo_key",
            "referencias",
            "itens",
            "mensalidades_pagas",
            "mensalidades_total",
            "numero_ciclos",
            "refinanciamento_numero",
            "pagamento_status",
            "legacy_refinanciamento_id",
            "origem",
            "data_anexo_associado",
            "data_anexo_agente",
            "data_pagamento_associado",
            "data_pagamento_agente",
            "data_renovacao",
            "origem_renovacao",
            "motivo_apto_renovacao",
            "data_primeiro_ciclo_ativado",
            "data_ativacao_ciclo",
            "origem_data_ativacao",
            "data_solicitacao_renovacao",
            "data_solicitacao",
            "ativacao_inferida",
            "etapa_operacional",
            "motivo_bloqueio",
            "observacao",
            "analista_note",
            "coordenador_note",
            "reviewed_at",
            "executado_em",
            "created_at",
            "updated_at",
            "auditoria",
            "comprovantes",
        ]

    def _itens_refinanciamento(self, obj: Refinanciamento):
        prefetched = getattr(obj, "_prefetched_objects_cache", {})
        itens = list(prefetched.get("itens", []))
        if itens:
            return sorted(itens, key=lambda item: (item.referencia_month, item.id))
        return list(obj.itens.all().order_by("referencia_month", "id"))

    def get_ciclo_key(self, obj: Refinanciamento) -> str:
        if obj.cycle_key:
            return obj.cycle_key
        referencias = [
            item.referencia_month.strftime("%Y-%m")
            for item in self._itens_refinanciamento(obj)
        ]
        return "|".join(referencias)

    def get_referencias(self, obj: Refinanciamento) -> list[str]:
        itens = self._itens_refinanciamento(obj)
        if itens:
            return [item.referencia_month.isoformat() for item in itens]
        return [
            referencia.isoformat()
            for referencia in [obj.ref1, obj.ref2, obj.ref3, obj.ref4]
            if referencia is not None
        ]

    @extend_schema_field(RefinanciamentoItemResumoSerializer(many=True))
    def get_itens(self, obj: Refinanciamento) -> list[dict[str, object]]:
        return [
            {
                "id": item.id,
                "numero": indice,
                "pagamento_mensalidade_id": item.pagamento_mensalidade_id,
                "referencia_mes": item.referencia_month,
                "valor": item.valor,
                "status": item.status_code or "1",
            }
            for indice, item in enumerate(self._itens_refinanciamento(obj), start=1)
        ]

    def get_mensalidades_pagas(self, obj: Refinanciamento) -> int:
        total = self.get_mensalidades_total(obj)
        itens = self._itens_refinanciamento(obj)
        if itens:
            return min(len(itens), total)
        return min(obj.parcelas_ok, total)

    @extend_schema_field(serializers.DecimalField(max_digits=10, decimal_places=2))
    def get_valor_liberado_associado(self, obj: Refinanciamento) -> str:
        contrato = self._resolve_contrato_origem(obj)
        valor = obj.valor_refinanciamento or Decimal("0.00")
        if (
            contrato is not None
            and contrato.margem_disponivel
            and contrato.margem_disponivel > 0
        ):
            valor = contrato.margem_disponivel
        elif valor <= 0 and contrato is not None:
            valor = (
                contrato.valor_liquido
                or contrato.valor_total_antecipacao
                or contrato.valor_mensalidade
                or Decimal("0.00")
            )
        return f"{valor:.2f}"

    @extend_schema_field(serializers.DecimalField(max_digits=10, decimal_places=2))
    def get_repasse_agente(self, obj: Refinanciamento) -> str:
        repasse = obj.repasse_agente or Decimal("0.00")
        if repasse > 0:
            return f"{repasse:.2f}"
        contrato = self._resolve_contrato_origem(obj)
        if contrato is not None and contrato.comissao_agente and contrato.comissao_agente > 0:
            repasse = contrato.comissao_agente
        return f"{repasse:.2f}"

    def _resolve_contrato_origem(self, obj: Refinanciamento):
        return _resolve_contrato(obj)

    def _latest_payment_comprovante(
        self,
        obj: Refinanciamento,
        papel: str,
    ) -> Comprovante | None:
        prefetched = getattr(obj, "_prefetched_objects_cache", {})
        comprovantes = list(prefetched.get("comprovantes", []))
        if not comprovantes:
            comprovantes = list(obj.comprovantes.all())
        comprovantes = [
            comprovante
            for comprovante in comprovantes
            if comprovante.papel == papel
            and comprovante.tipo
            in {
                Comprovante.Tipo.COMPROVANTE_PAGAMENTO_ASSOCIADO,
                Comprovante.Tipo.COMPROVANTE_PAGAMENTO_AGENTE,
            }
        ]
        if not comprovantes:
            return None
        comprovantes.sort(
            key=lambda item: (
                item.updated_at or item.created_at,
                item.created_at,
                item.id,
            ),
            reverse=True,
        )
        return comprovantes[0]

    def get_data_anexo_associado(self, obj: Refinanciamento):
        comprovante = self._latest_payment_comprovante(obj, Comprovante.Papel.ASSOCIADO)
        return (
            comprovante.updated_at or comprovante.created_at
            if comprovante is not None
            else None
        )

    def get_data_anexo_agente(self, obj: Refinanciamento):
        comprovante = self._latest_payment_comprovante(obj, Comprovante.Papel.AGENTE)
        return (
            comprovante.updated_at or comprovante.created_at
            if comprovante is not None
            else None
        )

    def get_data_pagamento_associado(self, obj: Refinanciamento):
        comprovante = self._latest_payment_comprovante(obj, Comprovante.Papel.ASSOCIADO)
        return comprovante.data_pagamento if comprovante is not None else None

    def get_data_pagamento_agente(self, obj: Refinanciamento):
        comprovante = self._latest_payment_comprovante(obj, Comprovante.Papel.AGENTE)
        return comprovante.data_pagamento if comprovante is not None else None

    def get_mensalidades_total(self, obj: Refinanciamento) -> int:
        if obj.contrato_origem is not None:
            return get_contract_cycle_size(obj.contrato_origem)
        itens = self._itens_refinanciamento(obj)
        return max(len(itens), 3)

    def get_numero_ciclos(self, obj: Refinanciamento) -> int:
        if obj.ciclo_origem_id and obj.ciclo_origem is not None:
            return obj.ciclo_origem.numero or 1
        return 1

    def get_matricula_display(self, obj: Refinanciamento) -> str:
        return obj.associado.matricula_display

    @extend_schema_field(ComprovanteResumoSerializer(many=True))
    def get_comprovantes(self, obj: Refinanciamento):
        prefetched = getattr(obj, "_prefetched_objects_cache", {})
        comprovantes = list(prefetched.get("comprovantes", []))
        if not comprovantes:
            comprovantes = list(obj.comprovantes.all())

        request = self.context.get("request")
        user = getattr(request, "user", None)
        if user and user.is_authenticated and user.has_role("AGENTE") and not user.has_role("ADMIN"):
            comprovantes = [
                comprovante
                for comprovante in comprovantes
                if comprovante.papel == Comprovante.Papel.AGENTE
            ]

        return ComprovanteResumoSerializer(
            comprovantes,
            many=True,
            context=self.context,
        ).data

    def get_refinanciamento_numero(self, obj: Refinanciamento) -> int:
        return (
            obj.associado.refinanciamentos.filter(created_at__lte=obj.created_at).count()
        )

    @extend_schema_field(RefinanciamentoAuditoriaSerializer)
    def get_auditoria(self, obj: Refinanciamento) -> dict[str, object]:
        return {
            "solicitado_por": SimpleUserSerializer(obj.solicitado_por).data
            if obj.solicitado_por
            else None,
            "aprovado_por": SimpleUserSerializer(obj.aprovado_por).data
            if obj.aprovado_por
            else None,
            "bloqueado_por": SimpleUserSerializer(obj.bloqueado_por).data
            if obj.bloqueado_por
            else None,
            "efetivado_por": SimpleUserSerializer(obj.efetivado_por).data
            if obj.efetivado_por
            else None,
            "reviewed_by": SimpleUserSerializer(obj.reviewed_by).data
            if obj.reviewed_by
            else None,
            "reviewed_at": obj.reviewed_at,
            "analista_note": obj.analista_note,
            "coordenador_note": obj.coordenador_note,
            "observacao": obj.observacao,
            "motivo_bloqueio": obj.motivo_bloqueio,
        }

    def get_pagamento_status(self, obj: Refinanciamento) -> str:
        return (
            "efetivado"
            if get_refinanciamento_activation_info(obj).activated_at is not None
            else "pendente"
        )

    @extend_schema_field(serializers.DateTimeField(allow_null=True))
    def get_data_primeiro_ciclo_ativado(self, obj: Refinanciamento) -> datetime | None:
        if obj.contrato_origem is None:
            return None
        return get_contract_first_cycle_activation_info(obj.contrato_origem).activated_at

    @extend_schema_field(serializers.DateTimeField(allow_null=True))
    def get_data_renovacao(self, obj: Refinanciamento) -> datetime | None:
        return obj.data_ativacao_ciclo or obj.executado_em or obj.created_at

    def get_origem_renovacao(self, obj: Refinanciamento) -> str:
        return obj.origem

    @extend_schema_field(serializers.DateTimeField(allow_null=True))
    def get_data_ativacao_ciclo(self, obj: Refinanciamento) -> datetime | None:
        return get_refinanciamento_activation_info(obj).activated_at

    def get_origem_data_ativacao(self, obj: Refinanciamento) -> str:
        return get_refinanciamento_activation_info(obj).source

    @extend_schema_field(serializers.DateTimeField(allow_null=True))
    def get_data_solicitacao_renovacao(self, obj: Refinanciamento) -> datetime | None:
        return obj.created_at

    def get_ativacao_inferida(self, obj: Refinanciamento) -> bool:
        return get_refinanciamento_activation_info(obj).inferred

    def get_etapa_operacional(self, obj: Refinanciamento) -> str:
        return obj.status

    def get_motivo_apto_renovacao(self, obj: Refinanciamento) -> str:
        if obj.status == Refinanciamento.Status.SOLICITADO_PARA_LIQUIDACAO:
            return "Contrato encaminhado para a fila de liquidação; aguardando tratativa da tesouraria."
        if obj.status == Refinanciamento.Status.PENDENTE_TERMO_ANALISTA:
            return obj.coordenador_note or "Coordenação devolveu o termo para nova conferência do analista."
        if obj.status == Refinanciamento.Status.PENDENTE_TERMO_AGENTE:
            return obj.analista_note or "Analista devolveu o termo para correção do agente."
        total = self.get_mensalidades_total(obj)
        pagas = min(self.get_mensalidades_pagas(obj), total)
        ciclo_numero = obj.ciclo_origem.numero if obj.ciclo_origem_id else 1
        if total > 0 and pagas == total - 1:
            return (
                f"{pagas}/{total} parcelas quitadas; última em previsão; "
                f"ciclo {ciclo_numero} elegível"
            )
        if total > 0 and pagas >= total:
            return (
                f"{pagas}/{total} parcelas quitadas; renovação em andamento; "
                f"ciclo {ciclo_numero} elegível"
            )
        return (
            f"{pagas}/{total} parcelas quitadas; "
            f"ciclo {ciclo_numero} em acompanhamento"
        )


class RefinanciamentoDetailSerializer(RefinanciamentoListSerializer):
    pass


class BloquearRefinanciamentoSerializer(serializers.Serializer):
    motivo = serializers.CharField()


class DesativarRefinanciamentoSerializer(serializers.Serializer):
    motivo = serializers.CharField()


class SolicitarRefinanciamentoSerializer(serializers.Serializer):
    termo_antecipacao = serializers.FileField(required=True)


class SolicitarLiquidacaoRefinanciamentoSerializer(serializers.Serializer):
    pass


class EncaminharLiquidacaoRefinanciamentoSerializer(serializers.Serializer):
    observacao = serializers.CharField(required=False, allow_blank=True, default="")


class AprovarRefinanciamentoSerializer(serializers.Serializer):
    observacao = serializers.CharField(required=False, allow_blank=True, default="")


class EfetivarRefinanciamentoSerializer(serializers.Serializer):
    comprovante_associado = serializers.FileField(required=False, allow_null=True)
    comprovante_agente = serializers.FileField(required=False, allow_null=True)
    proximo_ciclo_parcelas = serializers.IntegerField(
        required=False,
        allow_null=True,
        min_value=3,
        max_value=4,
    )


class SubstituirComprovanteRefinanciamentoSerializer(serializers.Serializer):
    papel = serializers.ChoiceField(
        choices=[
            Comprovante.Papel.ASSOCIADO,
            Comprovante.Papel.AGENTE,
        ]
    )
    arquivo = serializers.FileField(required=True)


class SubstituirTermoAgenteRefinanciamentoSerializer(serializers.Serializer):
    arquivo = serializers.FileField(required=True)


class AprovarAnaliseRefinanciamentoSerializer(serializers.Serializer):
    observacao = serializers.CharField(required=False, allow_blank=True, default="")


class DevolverRefinanciamentoSerializer(serializers.Serializer):
    observacao = serializers.CharField()


class AprovarEmMassaRefinanciamentoSerializer(serializers.Serializer):
    ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        allow_empty=False,
    )
    confirm_text = serializers.CharField()

    def validate_confirm_text(self, value: str) -> str:
        if value.strip().upper() != "CONFIRMAR":
            raise serializers.ValidationError(
                "Digite CONFIRMAR para executar a aprovação em massa."
            )
        return value


class ElegibilidadeRefinanciamentoSerializer(serializers.Serializer):
    elegivel = serializers.BooleanField()
    motivo = serializers.CharField()
    parcelas_pagas = serializers.IntegerField()
    mensalidades_livres = serializers.IntegerField()
    tem_refinanciamento_ativo = serializers.BooleanField()
