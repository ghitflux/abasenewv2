from django.conf import settings
from django.db import models
from django.utils import timezone

from core.models import BaseModel


class EsteiraItem(BaseModel):
    class Etapa(models.TextChoices):
        CADASTRO = "cadastro", "Cadastro"
        ANALISE = "analise", "Análise"
        COORDENACAO = "coordenacao", "Coordenação"
        TESOURARIA = "tesouraria", "Tesouraria"
        CONCLUIDO = "concluido", "Concluído"

    class Situacao(models.TextChoices):
        AGUARDANDO = "aguardando", "Aguardando"
        EM_ANDAMENTO = "em_andamento", "Em andamento"
        PENDENCIADO = "pendenciado", "Pendenciado"
        APROVADO = "aprovado", "Aprovado"
        REJEITADO = "rejeitado", "Rejeitado"

    associado = models.OneToOneField(
        "associados.Associado", on_delete=models.CASCADE, related_name="esteira_item"
    )
    etapa_atual = models.CharField(
        max_length=20, choices=Etapa.choices, default=Etapa.ANALISE
    )
    status_atual = models.CharField(
        max_length=20, choices=Etapa.choices, default=Etapa.ANALISE
    )
    status = models.CharField(
        max_length=20, choices=Situacao.choices, default=Situacao.AGUARDANDO
    )
    analista_responsavel = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="itens_analise",
    )
    coordenador_responsavel = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="itens_coordenacao",
    )
    tesoureiro_responsavel = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="itens_tesouraria",
    )
    prioridade = models.PositiveSmallIntegerField(default=3)
    assumido_em = models.DateTimeField(null=True, blank=True)
    heartbeat_at = models.DateTimeField(null=True, blank=True)
    concluido_em = models.DateTimeField(null=True, blank=True)
    observacao = models.TextField(blank=True)

    @property
    def analista(self):
        return self.analista_responsavel

    @property
    def coordenador(self):
        return self.coordenador_responsavel

    @property
    def tesoureiro(self):
        return self.tesoureiro_responsavel

    @property
    def ordem(self) -> int:
        return self.prioridade

    def save(self, *args, **kwargs):
        self.status_atual = self.etapa_atual
        if self.etapa_atual != self.Etapa.CONCLUIDO:
            self.concluido_em = None
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.associado.nome_completo} - {self.etapa_atual}:{self.status}"


class Transicao(BaseModel):
    esteira_item = models.ForeignKey(
        EsteiraItem, on_delete=models.CASCADE, related_name="transicoes"
    )
    acao = models.CharField(max_length=80, blank=True)
    de_status = models.CharField(max_length=20, choices=EsteiraItem.Etapa.choices)
    para_status = models.CharField(max_length=20, choices=EsteiraItem.Etapa.choices)
    de_situacao = models.CharField(
        max_length=20, choices=EsteiraItem.Situacao.choices, blank=True
    )
    para_situacao = models.CharField(
        max_length=20, choices=EsteiraItem.Situacao.choices, blank=True
    )
    realizado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="transicoes_realizadas",
    )
    observacao = models.TextField(blank=True)
    realizado_em = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-realizado_em"]


class Pendencia(BaseModel):
    class Status(models.TextChoices):
        ABERTA = "aberta", "Aberta"
        RESOLVIDA = "resolvida", "Resolvida"
        CANCELADA = "cancelada", "Cancelada"

    esteira_item = models.ForeignKey(
        EsteiraItem, on_delete=models.CASCADE, related_name="pendencias"
    )
    tipo = models.CharField(max_length=60, blank=True)
    descricao = models.TextField()
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.ABERTA
    )
    retornado_para_agente = models.BooleanField(default=True)
    resolvida_em = models.DateTimeField(null=True, blank=True)
    resolvida_por = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="pendencias_resolvidas",
    )

    class Meta:
        ordering = ["-created_at"]


class DocIssue(BaseModel):
    """Pendência documental apontada pelo analista (agente_doc_issues)."""

    class Status(models.TextChoices):
        INCOMPLETO = "incomplete", "Incompleto"
        RESOLVIDO = "resolved", "Resolvido"

    associado = models.ForeignKey(
        "associados.Associado",
        on_delete=models.PROTECT,
        related_name="doc_issues",
    )
    cpf_cnpj = models.CharField(max_length=20)
    contrato_codigo = models.CharField(max_length=80, blank=True)
    analista = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="doc_issues_criados",
    )
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.INCOMPLETO
    )
    mensagem = models.TextField()
    documents_snapshot_json = models.JSONField(null=True, blank=True)
    agent_uploads_json = models.JSONField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"DocIssue #{self.pk} - {self.associado_id} - {self.status}"


class DocReupload(BaseModel):
    """Arquivo reenviado pelo agente para resolver um DocIssue (agente_doc_reuploads)."""

    class Status(models.TextChoices):
        RECEBIDO = "received", "Recebido"
        ACEITO = "accepted", "Aceito"
        REJEITADO = "rejected", "Rejeitado"

    doc_issue = models.ForeignKey(
        DocIssue, on_delete=models.CASCADE, related_name="reuploads"
    )
    associado = models.ForeignKey(
        "associados.Associado",
        on_delete=models.PROTECT,
        related_name="doc_reuploads",
    )
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="doc_reuploads",
    )
    cpf_cnpj = models.CharField(max_length=20)
    contrato_codigo = models.CharField(max_length=80, blank=True)
    file_original_name = models.CharField(max_length=255)
    file_stored_name = models.CharField(max_length=255)
    file_relative_path = models.CharField(max_length=500, blank=True)
    file_mime = models.CharField(max_length=100, blank=True)
    file_size_bytes = models.BigIntegerField(null=True, blank=True)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.RECEBIDO
    )
    uploaded_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True)
    extras = models.JSONField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"DocReupload #{self.pk} - {self.file_original_name}"
