from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Role, User
from apps.associados.admin_override_service import AdminOverrideService
from apps.associados.models import Associado
from apps.contratos.cycle_rebuild import rebuild_contract_cycle_state
from apps.contratos.models import Contrato
from apps.importacao.models import PagamentoMensalidade
from apps.refinanciamento.models import Refinanciamento
from apps.refinanciamento.services import RefinanciamentoService
from apps.tesouraria.models import Pagamento


class CicloQuatroParcelasTestCase(TestCase):
    """Cobertura end-to-end para o ciclo de 4 parcelas.

    Valida que contratos com prazo_meses=4 fluem corretamente pelo pipeline:
    apto_a_renovar -> em_analise_renovacao -> visível para o analista,
    e que o editor avançado pode alternar entre ciclo de 3 e 4 parcelas.
    """

    @classmethod
    def setUpTestData(cls):
        cls.role_admin = Role.objects.create(codigo="ADMIN", nome="Administrador")
        cls.role_agente = Role.objects.create(codigo="AGENTE", nome="Agente")
        cls.role_coord = Role.objects.create(codigo="COORDENADOR", nome="Coordenador")
        cls.role_analista = Role.objects.create(codigo="ANALISTA", nome="Analista")

        cls.admin = cls._create_user("admin4@abase.local", cls.role_admin, "Admin4")
        cls.agente = cls._create_user("agente4@abase.local", cls.role_agente, "Agente4")
        cls.coordenador = cls._create_user(
            "coord4@abase.local", cls.role_coord, "Coord4"
        )
        cls.analista = cls._create_user(
            "analista4@abase.local", cls.role_analista, "Analista4"
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
        self.admin_client = APIClient()
        self.admin_client.force_authenticate(self.admin)

        self.agent_client = APIClient()
        self.agent_client.force_authenticate(self.agente)

        self.coord_client = APIClient()
        self.coord_client.force_authenticate(self.coordenador)

        self.analyst_client = APIClient()
        self.analyst_client.force_authenticate(self.analista)

    def _create_contrato_ciclo_4(self, cpf: str = "99988877766") -> Contrato:
        associado = Associado.objects.create(
            nome_completo="Associado Ciclo 4",
            cpf_cnpj=cpf,
            email=f"{cpf}@teste.local",
            telefone="86988887777",
            orgao_publico="SEFAZ",
            matricula_orgao=f"M4-{cpf[-4:]}",
            status=Associado.Status.ATIVO,
            agente_responsavel=self.agente,
        )
        contrato = Contrato.objects.create(
            associado=associado,
            agente=self.agente,
            valor_bruto=Decimal("2000.00"),
            valor_liquido=Decimal("1600.00"),
            margem_disponivel=Decimal("900.00"),
            valor_mensalidade=Decimal("500.00"),
            prazo_meses=4,
            status=Contrato.Status.ATIVO,
            data_contrato=date(2025, 12, 15),
            data_aprovacao=date(2025, 12, 20),
            data_primeira_mensalidade=date(2026, 1, 1),
        )
        Pagamento.objects.create(
            cadastro=associado,
            created_by=self.admin,
            contrato_codigo=contrato.codigo,
            contrato_valor_antecipacao=contrato.valor_liquido,
            contrato_margem_disponivel=Decimal("900.00"),
            cpf_cnpj=associado.cpf_cnpj,
            full_name=associado.nome_completo,
            agente_responsavel=self.agente.full_name,
            status=Pagamento.Status.PAGO,
            valor_pago=contrato.valor_liquido,
            paid_at=timezone.make_aware(
                datetime.combine(date(2025, 12, 20), datetime.min.time())
            ),
            forma_pagamento="pix",
        )
        return contrato

    def _create_pagamento(
        self,
        contrato: Contrato,
        referencia: date,
        *,
        status_code: str = "1",
    ) -> PagamentoMensalidade:
        return PagamentoMensalidade.objects.create(
            created_by=self.admin,
            import_uuid=f"uuid4-{contrato.id}-{referencia.isoformat()}",
            referencia_month=referencia,
            status_code=status_code,
            matricula=contrato.associado.matricula_orgao,
            orgao_pagto="SEFAZ",
            nome_relatorio=contrato.associado.nome_completo,
            cpf_cnpj=contrato.associado.cpf_cnpj,
            associado=contrato.associado,
            valor=contrato.valor_mensalidade,
            source_file_path=f"retornos/{referencia.strftime('%Y-%m')}.txt",
        )

    def _termo_file(self) -> SimpleUploadedFile:
        return SimpleUploadedFile(
            "termo.pdf", b"arquivo termo ciclo 4", content_type="application/pdf"
        )

    # ----------------------------- fluxo principal -----------------------------

    def test_contrato_ciclo_4_apto_apos_3_parcelas_pagas(self):
        """Contrato ciclo 4 deve ficar APTO_A_RENOVAR com 3 parcelas pagas (threshold = cycle_size - 1)."""
        contrato = self._create_contrato_ciclo_4()
        self._create_pagamento(contrato, date(2026, 1, 1))
        self._create_pagamento(contrato, date(2026, 2, 1))
        self._create_pagamento(contrato, date(2026, 3, 1))

        rebuild_contract_cycle_state(contrato, execute=True)
        refi = RefinanciamentoService._active_operational_refinanciamento(contrato)
        self.assertIsNotNone(refi, "Refinanciamento operacional deveria ter sido criado")
        self.assertEqual(refi.status, Refinanciamento.Status.APTO_A_RENOVAR)
        self.assertEqual(refi.parcelas_ok, 3)

    def test_solicitar_envia_para_analista_em_ciclo_4(self):
        """Após solicitar, refi de ciclo 4 deve ficar em EM_ANALISE_RENOVACAO e aparecer para analista."""
        contrato = self._create_contrato_ciclo_4(cpf="88877766655")
        self._create_pagamento(contrato, date(2026, 1, 1))
        self._create_pagamento(contrato, date(2026, 2, 1))
        self._create_pagamento(contrato, date(2026, 3, 1))

        response = self.admin_client.post(
            f"/api/v1/refinanciamentos/{contrato.id}/solicitar/",
            {"termo_antecipacao": self._termo_file()},
            format="multipart",
        )
        self.assertEqual(response.status_code, 201, response.json())
        refi = Refinanciamento.objects.get(pk=response.json()["id"])
        self.assertEqual(refi.status, Refinanciamento.Status.EM_ANALISE_RENOVACAO)
        self.assertEqual(refi.parcelas_ok, 3)

        list_response = self.analyst_client.get("/api/v1/refinanciamentos/")
        self.assertEqual(list_response.status_code, 200, list_response.json())
        ids = [item["id"] for item in list_response.json().get("results", [])]
        self.assertIn(
            refi.id,
            ids,
            "Analista deveria enxergar o refinanciamento ciclo 4 em análise.",
        )

        analise_response = self.analyst_client.get(
            "/api/v1/analise/refinanciamentos/"
        )
        self.assertEqual(
            analise_response.status_code, 200, analise_response.json()
        )
        analise_ids = [
            item["id"] for item in analise_response.json().get("results", [])
        ]
        self.assertIn(
            refi.id,
            analise_ids,
            "Endpoint /analise/refinanciamentos/ deveria listar refi ciclo 4.",
        )

    def test_contrato_ciclo_4_nao_apto_com_so_2_parcelas(self):
        """Ciclo 4 com apenas 2 parcelas pagas NÃO deve disparar refi operacional."""
        contrato = self._create_contrato_ciclo_4(cpf="77766655544")
        self._create_pagamento(contrato, date(2026, 1, 1))
        self._create_pagamento(contrato, date(2026, 2, 1))

        rebuild_contract_cycle_state(contrato, execute=True)
        refi = RefinanciamentoService._active_operational_refinanciamento(contrato)
        self.assertIsNone(
            refi, "Refi não deveria ser criado com paid_count < threshold (3)."
        )

    # ----------------------- editor avançado: 3 <-> 4 ---------------------------

    def test_admin_pode_alterar_prazo_meses_de_3_para_4(self):
        """Admin override aceita prazo_meses=4 e dispara rebuild da projeção."""
        associado = Associado.objects.create(
            nome_completo="Cadastro Alterar Ciclo",
            cpf_cnpj="11122233399",
            email="alt-ciclo@teste.local",
            telefone="86955554444",
            orgao_publico="SEFAZ",
            matricula_orgao="MAT-XYZ",
            status=Associado.Status.ATIVO,
            agente_responsavel=self.agente,
        )
        contrato = Contrato.objects.create(
            associado=associado,
            agente=self.agente,
            valor_bruto=Decimal("1500.00"),
            valor_liquido=Decimal("1200.00"),
            margem_disponivel=Decimal("900.00"),
            valor_mensalidade=Decimal("500.00"),
            prazo_meses=3,
            status=Contrato.Status.ATIVO,
            data_contrato=date(2025, 12, 15),
            data_aprovacao=date(2025, 12, 20),
            data_primeira_mensalidade=date(2026, 1, 1),
        )
        Pagamento.objects.create(
            cadastro=associado,
            created_by=self.admin,
            contrato_codigo=contrato.codigo,
            contrato_valor_antecipacao=contrato.valor_liquido,
            cpf_cnpj=associado.cpf_cnpj,
            full_name=associado.nome_completo,
            status=Pagamento.Status.PAGO,
            valor_pago=contrato.valor_liquido,
            paid_at=timezone.make_aware(
                datetime.combine(date(2025, 12, 20), datetime.min.time())
            ),
            forma_pagamento="pix",
        )

        AdminOverrideService.apply_contract_core_override(
            contrato=contrato,
            payload={
                "motivo": "Ajuste para ciclo de 4 parcelas",
                "prazo_meses": 4,
                "updated_at": contrato.updated_at,
            },
            user=self.admin,
        )
        contrato.refresh_from_db()
        self.assertEqual(contrato.prazo_meses, 4)

    def test_admin_rejeita_prazo_meses_invalido(self):
        """Serializer/serviço devem rejeitar prazo_meses fora de {3, 4}."""
        associado = Associado.objects.create(
            nome_completo="Cadastro Recusa Ciclo",
            cpf_cnpj="22233344455",
            email="rej-ciclo@teste.local",
            telefone="86944443333",
            orgao_publico="SEFAZ",
            matricula_orgao="MAT-ZZZ",
            status=Associado.Status.ATIVO,
            agente_responsavel=self.agente,
        )
        contrato = Contrato.objects.create(
            associado=associado,
            agente=self.agente,
            valor_bruto=Decimal("1500.00"),
            valor_liquido=Decimal("1200.00"),
            margem_disponivel=Decimal("900.00"),
            valor_mensalidade=Decimal("500.00"),
            prazo_meses=3,
            status=Contrato.Status.ATIVO,
            data_contrato=date(2025, 12, 15),
            data_aprovacao=date(2025, 12, 20),
            data_primeira_mensalidade=date(2026, 1, 1),
        )

        from rest_framework.exceptions import ValidationError

        with self.assertRaises(ValidationError):
            AdminOverrideService.apply_contract_core_override(
                contrato=contrato,
                payload={
                    "motivo": "Tentativa inválida",
                    "prazo_meses": 5,
                    "updated_at": contrato.updated_at,
                },
                user=self.admin,
            )
