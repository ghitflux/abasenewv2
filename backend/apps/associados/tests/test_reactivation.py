import tempfile
from datetime import date
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Role, User
from apps.associados.models import Associado
from apps.contratos.competencia import create_cycle_with_parcelas
from apps.contratos.models import Ciclo, Contrato, Parcela
from apps.esteira.models import EsteiraItem
from apps.esteira.services import EsteiraService
from apps.refinanciamento.models import Comprovante


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class AssociadoReactivationTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.role_admin = Role.objects.create(codigo="ADMIN", nome="Administrador")
        cls.role_coord = Role.objects.create(
            codigo="COORDENADOR",
            nome="Coordenador",
        )
        cls.role_tes = Role.objects.create(codigo="TESOUREIRO", nome="Tesoureiro")
        cls.role_agente = Role.objects.create(codigo="AGENTE", nome="Agente")
        cls.role_analista = Role.objects.create(codigo="ANALISTA", nome="Analista")

        cls.admin = cls._create_user("admin.reativacao@teste.local", cls.role_admin, "Admin")
        cls.coordenador = cls._create_user(
            "coord.reativacao@teste.local",
            cls.role_coord,
            "Coord",
        )
        cls.tesoureiro = cls._create_user(
            "tes.reativacao@teste.local",
            cls.role_tes,
            "Tesouraria",
        )
        cls.analista = cls._create_user(
            "analista.reativacao@teste.local",
            cls.role_analista,
            "Analista",
        )
        cls.agente_a = cls._create_user(
            "agente.a@teste.local",
            cls.role_agente,
            "Agente A",
        )
        cls.agente_b = cls._create_user(
            "agente.b@teste.local",
            cls.role_agente,
            "Agente B",
        )

    @classmethod
    def _create_user(cls, email: str, role: Role, first_name: str) -> User:
        user = User.objects.create_user(
            email=email,
            password="Senha@123",
            first_name=first_name,
            last_name="ABASE",
            is_active=True,
        )
        user.roles.add(role)
        return user

    def setUp(self):
        self.coord_client = APIClient()
        self.coord_client.force_authenticate(self.coordenador)

        self.tes_client = APIClient()
        self.tes_client.force_authenticate(self.tesoureiro)

        self.analyst_client = APIClient()
        self.analyst_client.force_authenticate(self.analista)

    def _create_inactive_associado(
        self,
        *,
        cpf: str,
        competencia_inicial: date = date(2025, 10, 1),
    ) -> Associado:
        data_aprovacao = competencia_inicial.replace(day=1)
        associado = Associado.objects.create(
            nome_completo="Associado Inativo",
            cpf_cnpj=cpf,
            telefone="86999999999",
            email="inativo@teste.local",
            orgao_publico="SEFAZ",
            status=Associado.Status.INATIVO,
            agente_responsavel=self.agente_a,
            auxilio_taxa=Decimal("10.00"),
        )
        contrato = Contrato.objects.create(
            associado=associado,
            agente=self.agente_a,
            status=Contrato.Status.CANCELADO,
            origem_operacional=Contrato.OrigemOperacional.CADASTRO,
            valor_bruto=Decimal("1500.00"),
            valor_liquido=Decimal("1200.00"),
            valor_mensalidade=Decimal("500.00"),
            prazo_meses=3,
            taxa_antecipacao=Decimal("30.00"),
            margem_disponivel=Decimal("1050.00"),
            valor_total_antecipacao=Decimal("1500.00"),
            doacao_associado=Decimal("450.00"),
            comissao_agente=Decimal("105.00"),
            data_aprovacao=data_aprovacao,
            data_primeira_mensalidade=competencia_inicial.replace(day=5),
            mes_averbacao=data_aprovacao,
            cancelado_em=timezone.now(),
        )
        create_cycle_with_parcelas(
            contrato=contrato,
            numero=1,
            competencia_inicial=competencia_inicial,
            parcelas_total=3,
            ciclo_status=Ciclo.Status.FECHADO,
            parcela_status=Parcela.Status.DESCONTADO,
            valor_mensalidade=Decimal("500.00"),
            valor_total=Decimal("1500.00"),
        )
        EsteiraItem.objects.create(
            associado=associado,
            etapa_atual=EsteiraItem.Etapa.CONCLUIDO,
            status=EsteiraItem.Situacao.REJEITADO,
            concluido_em=timezone.now(),
            observacao="Contrato anterior finalizado.",
        )
        return associado

    def _create_inactive_associado_without_contract(self, *, cpf: str) -> Associado:
        return Associado.objects.create(
            nome_completo="Associado Inativo Sem Contrato",
            cpf_cnpj=cpf,
            telefone="86999999999",
            email=f"sem-contrato-{cpf}@teste.local",
            orgao_publico="SEFAZ",
            status=Associado.Status.INATIVO,
            agente_responsavel=self.agente_a,
            auxilio_taxa=Decimal("10.00"),
        )

    def _reativacao_payload(self, *, agente_id: int, percentual_repasse: str | None = None):
        payload = {
            "valor_bruto_total": "1500.00",
            "valor_liquido": "1200.00",
            "prazo_meses": 3,
            "mensalidade": "500.00",
            "data_aprovacao": "2026-04-21",
            "agente_responsavel_id": agente_id,
        }
        if percentual_repasse is not None:
            payload["percentual_repasse"] = percentual_repasse
        return payload

    def test_reativacao_cria_novo_contrato_e_reusa_item_da_esteira(self):
        associado = self._create_inactive_associado(cpf="70000000001")
        esteira_id = associado.esteira_item.id
        contrato_anterior = associado.contratos.order_by("-id").first()

        response = self.coord_client.post(
            f"/api/v1/associados/{associado.id}/reativar/",
            self._reativacao_payload(
                agente_id=self.agente_b.id,
                percentual_repasse="14.00",
            ),
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.json())
        associado.refresh_from_db()
        novo_contrato = associado.contratos.order_by("-id").first()
        contrato_anterior.refresh_from_db()
        self.assertIsNotNone(novo_contrato)
        self.assertNotEqual(novo_contrato.id, contrato_anterior.id)
        self.assertEqual(contrato_anterior.status, Contrato.Status.ENCERRADO)
        self.assertEqual(contrato_anterior.cancelamento_tipo, "")
        self.assertEqual(contrato_anterior.cancelamento_motivo, "")
        self.assertIsNone(contrato_anterior.cancelado_em)
        self.assertEqual(associado.status, Associado.Status.EM_ANALISE)
        self.assertEqual(associado.agente_responsavel, self.agente_b)
        self.assertEqual(associado.auxilio_taxa, Decimal("14.00"))
        self.assertEqual(novo_contrato.agente, self.agente_b)
        self.assertEqual(novo_contrato.status, Contrato.Status.EM_ANALISE)
        self.assertEqual(
            novo_contrato.origem_operacional,
            Contrato.OrigemOperacional.REATIVACAO,
        )
        self.assertEqual(novo_contrato.margem_disponivel, Decimal("1050.00"))
        self.assertEqual(novo_contrato.valor_total_antecipacao, Decimal("1500.00"))
        self.assertEqual(novo_contrato.doacao_associado, Decimal("450.00"))
        self.assertEqual(novo_contrato.comissao_agente, Decimal("147.00"))
        self.assertEqual(novo_contrato.ciclos.count(), 0)

        associado.esteira_item.refresh_from_db()
        self.assertEqual(associado.esteira_item.id, esteira_id)
        self.assertEqual(associado.esteira_item.etapa_atual, EsteiraItem.Etapa.ANALISE)
        self.assertEqual(associado.esteira_item.status, EsteiraItem.Situacao.AGUARDANDO)

    def test_reativacao_sem_contrato_anterior_cria_fluxo_e_efetiva_ciclo(self):
        associado = self._create_inactive_associado_without_contract(cpf="70000000008")

        response = self.coord_client.post(
            f"/api/v1/associados/{associado.id}/reativar/",
            self._reativacao_payload(agente_id=self.agente_a.id),
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.json())
        associado.refresh_from_db()
        novo_contrato = associado.contratos.get()
        self.assertEqual(associado.status, Associado.Status.EM_ANALISE)
        self.assertEqual(
            novo_contrato.origem_operacional,
            Contrato.OrigemOperacional.REATIVACAO,
        )
        self.assertEqual(novo_contrato.ciclos.count(), 0)
        self.assertEqual(associado.esteira_item.etapa_atual, EsteiraItem.Etapa.ANALISE)
        self.assertEqual(associado.esteira_item.status, EsteiraItem.Situacao.AGUARDANDO)

        EsteiraService.assumir(associado.esteira_item, self.analista)
        EsteiraService.aprovar(associado.esteira_item, self.analista)

        list_response = self.tes_client.get(
            "/api/v1/tesouraria/contratos/",
            {"pagamento": "pendente", "origem_operacional": "reativacao"},
        )
        self.assertEqual(list_response.status_code, 200, list_response.json())
        row = next(
            item
            for item in list_response.json()["results"]
            if item["id"] == novo_contrato.id
        )
        self.assertIsNone(row["reactivation_cycle_preview"]["ultima_parcela_paga"])
        self.assertEqual(
            row["reactivation_cycle_preview"]["competencias_sugeridas"],
            ["2026-06-01", "2026-07-01", "2026-08-01"],
        )

        for papel in ["associado", "agente"]:
            upload_response = self.tes_client.post(
                f"/api/v1/tesouraria/contratos/{novo_contrato.id}/substituir-comprovante/",
                {
                    "papel": papel,
                    "arquivo": SimpleUploadedFile(
                        f"{papel}.pdf",
                        b"arquivo",
                        content_type="application/pdf",
                    ),
                },
                format="multipart",
            )
            self.assertEqual(upload_response.status_code, 200, upload_response.json())

        efetivar = self.tes_client.post(
            f"/api/v1/tesouraria/contratos/{novo_contrato.id}/efetivar/",
            {},
            format="json",
        )
        self.assertEqual(efetivar.status_code, 200, efetivar.json())

        associado.refresh_from_db()
        novo_contrato.refresh_from_db()
        ciclo_novo = novo_contrato.ciclos.get()
        self.assertEqual(associado.status, Associado.Status.ATIVO)
        self.assertEqual(novo_contrato.status, Contrato.Status.ATIVO)
        self.assertEqual(ciclo_novo.status, Ciclo.Status.ABERTO)
        self.assertEqual(
            list(ciclo_novo.parcelas.order_by("referencia_mes").values_list("referencia_mes", flat=True)),
            [date(2026, 6, 1), date(2026, 7, 1), date(2026, 8, 1)],
        )

    def test_reativacao_aparece_na_secao_propria_mesmo_com_historico_mais_novo(self):
        associado = self._create_inactive_associado(cpf="70000000007")

        response = self.coord_client.post(
            f"/api/v1/associados/{associado.id}/reativar/",
            self._reativacao_payload(agente_id=self.agente_a.id),
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.json())
        reativacao = associado.contratos.order_by("-id").first()

        Contrato.objects.create(
            associado=associado,
            agente=self.agente_a,
            status=Contrato.Status.CANCELADO,
            origem_operacional=Contrato.OrigemOperacional.CADASTRO,
            valor_bruto=Decimal("1500.00"),
            valor_liquido=Decimal("1200.00"),
            valor_mensalidade=Decimal("500.00"),
            prazo_meses=3,
            taxa_antecipacao=Decimal("30.00"),
            margem_disponivel=Decimal("1050.00"),
            valor_total_antecipacao=Decimal("1500.00"),
            doacao_associado=Decimal("450.00"),
            comissao_agente=Decimal("105.00"),
            data_aprovacao=date(2026, 4, 22),
            data_primeira_mensalidade=date(2026, 5, 5),
            mes_averbacao=date(2026, 4, 1),
            cancelado_em=timezone.now(),
        )
        Associado.objects.filter(pk=associado.pk).update(
            status=Associado.Status.INATIVO,
            updated_at=timezone.now(),
        )

        response = self.analyst_client.get(
            "/api/v1/analise/filas/?secao=contratos_reativacao"
        )
        self.assertEqual(response.status_code, 200, response.json())
        rows = response.json()["results"]
        self.assertIn(associado.esteira_item.id, {row["id"] for row in rows})
        row = next(item for item in rows if item["id"] == associado.esteira_item.id)
        self.assertEqual(row["contrato"]["codigo"], reativacao.codigo)

        response = self.analyst_client.get(
            "/api/v1/analise/filas/?secao=novos_contratos"
        )
        self.assertEqual(response.status_code, 200, response.json())
        self.assertNotIn(
            associado.esteira_item.id,
            {row["id"] for row in response.json()["results"]},
        )

    def test_efetivacao_reativacao_cria_ciclo_abril_maio_junho(self):
        associado = self._create_inactive_associado(
            cpf="70000000006",
            competencia_inicial=date(2026, 1, 1),
        )

        response = self.coord_client.post(
            f"/api/v1/associados/{associado.id}/reativar/",
            self._reativacao_payload(agente_id=self.agente_a.id),
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.json())
        associado.refresh_from_db()
        novo_contrato = associado.contratos.order_by("-id").first()
        self.assertEqual(novo_contrato.ciclos.count(), 0)

        EsteiraService.assumir(associado.esteira_item, self.analista)
        EsteiraService.aprovar(associado.esteira_item, self.analista)

        list_response = self.tes_client.get(
            "/api/v1/tesouraria/contratos/",
            {"pagamento": "pendente", "origem_operacional": "reativacao"},
        )
        self.assertEqual(list_response.status_code, 200, list_response.json())
        row = list_response.json()["results"][0]
        self.assertEqual(
            row["reactivation_cycle_preview"]["ultima_parcela_paga"],
            "2026-03-01",
        )
        self.assertEqual(
            row["reactivation_cycle_preview"]["competencias_sugeridas"],
            ["2026-04-01", "2026-05-01", "2026-06-01"],
        )

        for papel in ["associado", "agente"]:
            upload_response = self.tes_client.post(
                f"/api/v1/tesouraria/contratos/{novo_contrato.id}/substituir-comprovante/",
                {
                    "papel": papel,
                    "arquivo": SimpleUploadedFile(
                        f"{papel}.pdf",
                        b"arquivo",
                        content_type="application/pdf",
                    ),
                },
                format="multipart",
            )
            self.assertEqual(upload_response.status_code, 200, upload_response.json())

        upload_response = self.tes_client.post(
            f"/api/v1/tesouraria/contratos/{novo_contrato.id}/substituir-comprovante/",
            {
                "papel": "associado",
                "arquivo": SimpleUploadedFile(
                    "associado-v2.pdf",
                    b"arquivo-v2",
                    content_type="application/pdf",
                ),
            },
            format="multipart",
        )
        self.assertEqual(upload_response.status_code, 200, upload_response.json())

        efetivar = self.tes_client.post(
            f"/api/v1/tesouraria/contratos/{novo_contrato.id}/efetivar/",
            {},
            format="json",
        )
        self.assertEqual(efetivar.status_code, 200, efetivar.json())

        associado.refresh_from_db()
        novo_contrato.refresh_from_db()
        ciclo_anterior = (
            associado.contratos.exclude(id=novo_contrato.id)
            .first()
            .ciclos.first()
        )
        ciclo_novo = novo_contrato.ciclos.get()
        self.assertEqual(associado.status, Associado.Status.ATIVO)
        self.assertEqual(novo_contrato.status, Contrato.Status.ATIVO)
        self.assertEqual(ciclo_anterior.status, Ciclo.Status.FECHADO)
        self.assertEqual(ciclo_novo.status, Ciclo.Status.ABERTO)
        self.assertEqual(
            list(ciclo_novo.parcelas.order_by("referencia_mes").values_list("referencia_mes", flat=True)),
            [date(2026, 4, 1), date(2026, 5, 1), date(2026, 6, 1)],
        )
        self.assertEqual(novo_contrato.comprovantes.count(), 3)
        self.assertEqual(
            novo_contrato.comprovantes.filter(
                papel=Comprovante.Papel.ASSOCIADO,
                deleted_at__isnull=True,
            ).count(),
            2,
        )
        self.assertEqual(
            novo_contrato.comprovantes.filter(
                papel=Comprovante.Papel.AGENTE,
                deleted_at__isnull=True,
            ).count(),
            1,
        )
        detail_response = self.coord_client.get(f"/api/v1/associados/{associado.id}/")
        self.assertEqual(detail_response.status_code, 200, detail_response.json())
        detail_contract = next(
            item
            for item in detail_response.json()["contratos"]
            if item["id"] == novo_contrato.id
        )
        self.assertEqual(len(detail_contract["pagamento_inicial_evidencias"]), 3)
        self.assertEqual(
            len(detail_contract["ciclos"][0]["comprovantes_ciclo"]),
            3,
        )
        self.assertTrue(
            associado.esteira_item.transicoes.filter(
                acao="reativar_associado"
            ).exists()
        )

    def test_efetivacao_reativacao_preserva_competencia_com_evidencia_historica(self):
        associado = self._create_inactive_associado(
            cpf="70000000009",
            competencia_inicial=date(2026, 1, 1),
        )
        contrato_anterior = associado.contratos.get()
        ciclo_futuro = Ciclo.objects.create(
            contrato=contrato_anterior,
            numero=2,
            data_inicio=date(2026, 4, 1),
            data_fim=date(2026, 6, 1),
            status=Ciclo.Status.APTO_A_RENOVAR,
            valor_total=Decimal("1500.00"),
        )
        parcela_abril_historica = Parcela.objects.create(
            ciclo=ciclo_futuro,
            associado=associado,
            numero=1,
            referencia_mes=date(2026, 4, 1),
            valor=Decimal("500.00"),
            data_vencimento=date(2026, 4, 5),
            status=Parcela.Status.EM_PREVISAO,
            data_pagamento=date(2026, 4, 10),
        )

        response = self.coord_client.post(
            f"/api/v1/associados/{associado.id}/reativar/",
            self._reativacao_payload(agente_id=self.agente_a.id),
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.json())
        associado.refresh_from_db()
        novo_contrato = associado.contratos.order_by("-id").first()

        EsteiraService.assumir(associado.esteira_item, self.analista)
        EsteiraService.aprovar(associado.esteira_item, self.analista)
        for papel in ["associado", "agente"]:
            upload_response = self.tes_client.post(
                f"/api/v1/tesouraria/contratos/{novo_contrato.id}/substituir-comprovante/",
                {
                    "papel": papel,
                    "arquivo": SimpleUploadedFile(
                        f"{papel}.pdf",
                        b"arquivo",
                        content_type="application/pdf",
                    ),
                },
                format="multipart",
            )
            self.assertEqual(upload_response.status_code, 200, upload_response.json())

        efetivar = self.tes_client.post(
            f"/api/v1/tesouraria/contratos/{novo_contrato.id}/efetivar/",
            {},
            format="json",
        )

        self.assertEqual(efetivar.status_code, 200, efetivar.json())
        parcela_abril_historica.refresh_from_db()
        self.assertIsNone(parcela_abril_historica.deleted_at)
        self.assertEqual(parcela_abril_historica.status, Parcela.Status.EM_PREVISAO)
        ciclo_novo = novo_contrato.ciclos.get()
        self.assertEqual(
            list(ciclo_novo.parcelas.order_by("referencia_mes").values_list("referencia_mes", flat=True)),
            [date(2026, 4, 1), date(2026, 5, 1), date(2026, 6, 1)],
        )
        parcelas_abril_ativas = Parcela.all_objects.filter(
            associado=associado,
            referencia_mes=date(2026, 4, 1),
            deleted_at__isnull=True,
        ).exclude(status=Parcela.Status.CANCELADO)
        self.assertEqual(parcelas_abril_ativas.count(), 2)
        self.assertTrue(
            parcelas_abril_ativas.filter(ciclo__contrato=contrato_anterior).exists()
        )
        self.assertTrue(
            parcelas_abril_ativas.filter(ciclo__contrato=novo_contrato).exists()
        )
        self.assertTrue(
            all(item.competencia_lock is None for item in parcelas_abril_ativas)
        )

    def test_reativacao_bloqueia_novo_fluxo_se_ja_existe_contrato_operacional(self):
        associado = self._create_inactive_associado(cpf="70000000002")
        Contrato.objects.create(
            associado=associado,
            agente=self.agente_a,
            status=Contrato.Status.EM_ANALISE,
            origem_operacional=Contrato.OrigemOperacional.CADASTRO,
            valor_bruto=Decimal("1500.00"),
            valor_liquido=Decimal("1200.00"),
            valor_mensalidade=Decimal("500.00"),
            prazo_meses=3,
            taxa_antecipacao=Decimal("30.00"),
            margem_disponivel=Decimal("1050.00"),
            valor_total_antecipacao=Decimal("1500.00"),
            doacao_associado=Decimal("450.00"),
            comissao_agente=Decimal("105.00"),
            data_aprovacao=date(2026, 4, 1),
            data_primeira_mensalidade=date(2026, 5, 5),
            mes_averbacao=date(2026, 4, 1),
        )

        response = self.coord_client.post(
            f"/api/v1/associados/{associado.id}/reativar/",
            self._reativacao_payload(agente_id=self.agente_a.id),
            format="json",
        )

        self.assertEqual(response.status_code, 400, response.json())
        self.assertEqual(
            response.json()["detail"],
            "Já existe um contrato operacional em andamento para este associado.",
        )

    def test_associado_inativo_exibe_contrato_historico_no_detalhe(self):
        associado = self._create_inactive_associado(cpf="70000000004")

        response = self.coord_client.get(f"/api/v1/associados/{associado.id}/")

        self.assertEqual(response.status_code, 200, response.json())
        contratos = response.json()["contratos"]
        self.assertEqual(len(contratos), 1)
        self.assertEqual(contratos[0]["status"], Contrato.Status.CANCELADO)
        self.assertEqual(len(contratos[0]["ciclos"]), 1)
        self.assertGreater(len(contratos[0]["ciclos"][0]["parcelas"]), 0)

    def test_reativacao_reusa_esteira_residual_sem_contrato_operacional(self):
        associado = self._create_inactive_associado(cpf="70000000005")
        esteira = associado.esteira_item
        esteira.etapa_atual = EsteiraItem.Etapa.ANALISE
        esteira.status = EsteiraItem.Situacao.EM_ANDAMENTO
        esteira.save(update_fields=["etapa_atual", "status", "updated_at"])

        response = self.coord_client.post(
            f"/api/v1/associados/{associado.id}/reativar/",
            self._reativacao_payload(agente_id=self.agente_a.id),
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.json())
        associado.esteira_item.refresh_from_db()
        self.assertEqual(associado.esteira_item.etapa_atual, EsteiraItem.Etapa.ANALISE)
        self.assertEqual(associado.esteira_item.status, EsteiraItem.Situacao.AGUARDANDO)

    def test_tesouraria_filtra_reativacoes_e_cancelamento_retorna_para_inativo(self):
        associado = self._create_inactive_associado(cpf="70000000003")

        response = self.coord_client.post(
            f"/api/v1/associados/{associado.id}/reativar/",
            self._reativacao_payload(agente_id=self.agente_a.id),
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.json())

        associado.refresh_from_db()
        contrato = associado.contratos.order_by("-id").first()

        list_response = self.tes_client.get(
            "/api/v1/tesouraria/contratos/",
            {
                "pagamento": "pendente",
                "origem_operacional": "reativacao",
            },
        )
        self.assertEqual(list_response.status_code, 200, list_response.json())
        self.assertEqual(list_response.json()["results"], [])

        EsteiraService.assumir(associado.esteira_item, self.analista)
        EsteiraService.aprovar(associado.esteira_item, self.analista)
        associado.esteira_item.refresh_from_db()
        self.assertEqual(associado.esteira_item.etapa_atual, EsteiraItem.Etapa.TESOURARIA)

        list_response = self.tes_client.get(
            "/api/v1/tesouraria/contratos/",
            {
                "pagamento": "pendente",
                "origem_operacional": "reativacao",
            },
        )
        self.assertEqual(list_response.status_code, 200, list_response.json())
        rows = list_response.json()["results"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], contrato.id)
        self.assertEqual(rows[0]["origem_operacional"], "reativacao")
        self.assertEqual(rows[0]["origem_operacional_label"], "Reativação")

        default_list_response = self.tes_client.get(
            "/api/v1/tesouraria/contratos/",
            {
                "pagamento": "pendente",
                "origem_operacional": "cadastro",
            },
        )
        self.assertEqual(default_list_response.status_code, 200, default_list_response.json())
        ids = {row["id"] for row in default_list_response.json()["results"]}
        self.assertNotIn(contrato.id, ids)

        cancel_response = self.tes_client.post(
            f"/api/v1/tesouraria/contratos/{contrato.id}/cancelar/",
            {
                "tipo": "cancelado",
                "motivo": "Reativação interrompida.",
            },
            format="json",
        )
        self.assertEqual(cancel_response.status_code, 200, cancel_response.json())
        associado.refresh_from_db()
        contrato.refresh_from_db()
        self.assertEqual(contrato.status, Contrato.Status.CANCELADO)
        self.assertEqual(associado.status, Associado.Status.INATIVO)
