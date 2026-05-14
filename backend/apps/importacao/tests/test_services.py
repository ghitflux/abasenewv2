from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.files.storage import default_storage
from django.db.models import Sum
from django.test import override_settings
from django.utils import timezone

from apps.associados.models import Associado
from apps.contratos.cycle_rebuild import rebuild_contract_cycle_state
from apps.contratos.models import Contrato, Parcela
from apps.refinanciamento.models import Refinanciamento
from apps.tesouraria.models import DevolucaoAssociado

from .base import ImportacaoBaseTestCase
from ..dry_run import simular_dry_run
from ..legacy import LegacyPagamentoSnapshot
from ..financeiro import build_financeiro_payload
from ..models import (
    ArquivoRetorno,
    ArquivoRetornoItem,
    DuplicidadeFinanceira,
    ImportacaoLog,
    PagamentoMensalidade,
)
from ..services import ArquivoRetornoService


def build_detail_line(
    status: str,
    matricula: str,
    nome: str,
    cargo: str,
    fin: str,
    orgao: str,
    lancamento: str,
    total_pago: str,
    valor: str,
    orgao_pagto: str,
    cpf: str,
) -> str:
    return (
        f"{status:>7}"
        f"{matricula:<10}"
        f"{nome:<31}"
        f"{cargo:<31}"
        f"{fin:>5}"
        f"{orgao:>6}"
        f"{lancamento:>7}"
        f"{total_pago:>12}"
        f"{valor:>13}"
        f"{orgao_pagto:>12}"
        f"{cpf}"
    )


@override_settings(CELERY_TASK_ALWAYS_EAGER=True)
class ArquivoRetornoServiceTestCase(ImportacaoBaseTestCase):
    def _dry_run_item(
        self,
        *,
        cpf: str,
        nome: str,
        status_codigo: str = "1",
        valor: Decimal = Decimal("30.00"),
    ) -> dict:
        return {
            "linha_numero": 1,
            "cpf_cnpj": cpf,
            "matricula_servidor": "MAT-RET",
            "nome_servidor": nome,
            "orgao_pagto_nome": "Órgão Teste",
            "orgao_pagto_codigo": "001",
            "orgao_codigo": "001",
            "valor_descontado": valor,
            "status_codigo": status_codigo,
        }

    def test_dry_run_marca_novo_apto_apos_baixa_da_competencia(self):
        self.create_associado_com_contrato(
            cpf="10000000001",
            nome="Servidor Novo Apto",
            competencia_final=date(2025, 5, 1),
            status_ultima_parcela=Parcela.Status.EM_ABERTO,
            valor_mensalidade=Decimal("150.00"),
        )

        payload = simular_dry_run(
            date(2025, 5, 1),
            [
                self._dry_run_item(
                    cpf="10000000001",
                    nome="Servidor Novo Apto",
                    valor=Decimal("150.00"),
                )
            ],
        )

        self.assertEqual(payload["kpis"]["aptos_a_renovar"], 1)
        self.assertTrue(payload["items"][0]["ficara_apto_renovar"])
        self.assertEqual(payload["items"][0]["ciclo_status_depois"], "apto_a_renovar")

    def test_dry_run_nao_conta_contrato_com_fluxo_renovacao_ja_ativo(self):
        associado, contrato, ciclo = self.create_associado_com_contrato(
            cpf="10000000002",
            nome="Servidor Ja Apto",
            competencia_final=date(2025, 5, 1),
            status_ultima_parcela=Parcela.Status.DESCONTADO,
            valor_mensalidade=Decimal("150.00"),
        )
        Refinanciamento.objects.create(
            associado=associado,
            contrato_origem=contrato,
            solicitado_por=self.tesoureiro,
            competencia_solicitada=date(2025, 6, 1),
            status=Refinanciamento.Status.PENDENTE_APTO,
            ciclo_origem=ciclo,
            valor_refinanciamento=Decimal("450.00"),
            repasse_agente=Decimal("45.00"),
        )

        payload = simular_dry_run(
            date(2025, 5, 1),
            [
                self._dry_run_item(
                    cpf="10000000002",
                    nome="Servidor Ja Apto",
                )
            ],
        )

        self.assertEqual(payload["kpis"]["aptos_a_renovar"], 0)
        self.assertFalse(payload["items"][0]["ficara_apto_renovar"])

    def test_dry_run_status_apto_do_associado_nao_bloqueia_nova_aptidao(self):
        associado, _contrato, _ciclo = self.create_associado_com_contrato(
            cpf="10000000005",
            nome="Servidor Status Derivado",
            competencia_final=date(2025, 5, 1),
            status_ultima_parcela=Parcela.Status.EM_ABERTO,
            valor_mensalidade=Decimal("150.00"),
        )
        associado.status = Associado.Status.APTO_A_RENOVAR
        associado.save(update_fields=["status", "updated_at"])

        payload = simular_dry_run(
            date(2025, 5, 1),
            [
                self._dry_run_item(
                    cpf="10000000005",
                    nome="Servidor Status Derivado",
                    valor=Decimal("150.00"),
                )
            ],
        )

        self.assertEqual(payload["kpis"]["aptos_a_renovar"], 1)
        self.assertTrue(payload["items"][0]["ficara_apto_renovar"])

    def test_dry_run_nao_gera_apto_para_retorno_nao_descontado(self):
        self.create_associado_com_contrato(
            cpf="10000000003",
            nome="Servidor Rejeitado",
            competencia_final=date(2025, 5, 1),
            status_ultima_parcela=Parcela.Status.EM_ABERTO,
        )

        payload = simular_dry_run(
            date(2025, 5, 1),
            [
                self._dry_run_item(
                    cpf="10000000003",
                    nome="Servidor Rejeitado",
                    status_codigo="2",
                )
            ],
        )

        self.assertEqual(payload["kpis"]["aptos_a_renovar"], 0)
        self.assertEqual(payload["items"][0]["resultado"], "nao_descontado")
        self.assertFalse(payload["items"][0]["ficara_apto_renovar"])

    def test_dry_run_nao_gera_apto_quando_retorno_vira_pendencia_manual(self):
        self.create_associado_com_contrato(
            cpf="10000000004",
            nome="Servidor Pendencia",
            competencia_final=date(2025, 5, 1),
            status_ultima_parcela=Parcela.Status.EM_ABERTO,
        )

        payload = simular_dry_run(
            date(2025, 5, 1),
            [
                self._dry_run_item(
                    cpf="10000000004",
                    nome="Servidor Pendencia",
                    valor=Decimal("31.00"),
                )
            ],
        )

        self.assertEqual(payload["kpis"]["aptos_a_renovar"], 0)
        self.assertEqual(payload["items"][0]["resultado"], "pendencia_manual")
        self.assertFalse(payload["items"][0]["ficara_apto_renovar"])

    def test_dry_run_nao_conta_valor_30_50_como_apto_renovar(self):
        self.create_associado_com_contrato(
            cpf="10000000006",
            nome="Servidor Pequeno Valor",
            competencia_final=date(2025, 5, 1),
            status_ultima_parcela=Parcela.Status.EM_ABERTO,
            valor_mensalidade=Decimal("30.00"),
        )

        payload = simular_dry_run(
            date(2025, 5, 1),
            [
                self._dry_run_item(
                    cpf="10000000006",
                    nome="Servidor Pequeno Valor",
                    valor=Decimal("30.00"),
                )
            ],
        )

        self.assertEqual(payload["kpis"]["aptos_a_renovar"], 0)
        self.assertFalse(payload["items"][0]["ficara_apto_renovar"])

    def test_dry_run_sinaliza_desconto_em_associado_inativo(self):
        associado, _contrato, _ciclo = self.create_associado_com_contrato(
            cpf="10000000007",
            nome="Servidor Inativo",
            competencia_final=date(2025, 5, 1),
            status_ultima_parcela=Parcela.Status.EM_ABERTO,
            valor_mensalidade=Decimal("150.00"),
        )
        associado.status = Associado.Status.INATIVO
        associado.save(update_fields=["status", "updated_at"])

        payload = simular_dry_run(
            date(2025, 5, 1),
            [
                self._dry_run_item(
                    cpf="10000000007",
                    nome="Servidor Inativo",
                    valor=Decimal("150.00"),
                )
            ],
        )

        self.assertEqual(payload["kpis"]["associados_inativos_com_desconto"], 1)
        self.assertTrue(payload["items"][0]["desconto_em_associado_inativo"])
        self.assertEqual(
            payload["items"][0]["associado_status_depois"],
            Associado.Status.INATIVO,
        )

    def test_upload_endpoint_restringe_permissao_e_processa_fixture(self):
        response = self.agent_client.get("/api/v1/importacao/arquivo-retorno/")
        self.assertEqual(response.status_code, 403)

        response = self.tes_client.get("/api/v1/importacao/arquivo-retorno/")
        self.assertEqual(response.status_code, 403)

        arquivo_agente = SimpleUploadedFile(
            "retorno_etipi_052025.txt",
            self.fixture_bytes(),
            content_type="text/plain",
        )
        response = self.agent_client.post(
            "/api/v1/importacao/arquivo-retorno/upload/",
            {"arquivo": arquivo_agente},
            format="multipart",
        )
        self.assertEqual(response.status_code, 403)

        arquivo_tes = SimpleUploadedFile(
            "retorno_etipi_052025.txt",
            self.fixture_bytes(),
            content_type="text/plain",
        )
        response = self.tes_client.post(
            "/api/v1/importacao/arquivo-retorno/upload/",
            {"arquivo": arquivo_tes},
            format="multipart",
        )
        self.assertEqual(response.status_code, 403)

        self.create_associado_com_contrato(
            cpf="23993596315",
            nome="Maria de Jesus Santana Costa",
        )
        self.create_associado_com_contrato(
            cpf="21819424391",
            nome="Francisco Crisostomo Batista",
        )
        self.create_associado_com_contrato(
            cpf="48204773315",
            nome="Maria de Jesus Araujo Goncalves",
        )

        arquivo_coord = SimpleUploadedFile(
            "retorno_etipi_052025.txt",
            self.fixture_bytes(),
            content_type="text/plain",
        )
        response = self.coord_client.post(
            "/api/v1/importacao/arquivo-retorno/upload/",
            {"arquivo": arquivo_coord},
            format="multipart",
        )
        self.assertEqual(response.status_code, 201, response.json())
        payload = response.json()

        self.assertEqual(payload["competencia_display"], "05/2025")
        self.assertEqual(payload["status"], ArquivoRetorno.Status.AGUARDANDO_CONFIRMACAO)
        self.assertEqual(payload["dry_run_resultado"]["kpis"]["associados_importados"], 1)

        response = self.coord_client.post(
            f"/api/v1/importacao/arquivo-retorno/{payload['id']}/confirmar/"
        )
        self.assertEqual(response.status_code, 200, response.json())
        payload = response.json()

        self.assertEqual(payload["status"], ArquivoRetorno.Status.CONCLUIDO)
        self.assertEqual(payload["resumo"]["baixa_efetuada"], 2)
        self.assertEqual(payload["resumo"]["nao_descontado"], 2)
        self.assertEqual(payload["resumo"]["pendencias_manuais"], 0)
        self.assertEqual(payload["resumo"]["nao_encontrado"], 0)
        self.assertEqual(payload["resumo"]["associados_importados"], 1)
        self.assertEqual(payload["financeiro"]["total"], 4)
        self.assertEqual(payload["financeiro"]["ok"], 2)
        self.assertEqual(payload["financeiro"]["recebido"], "60.00")
        associado_importado = Associado.objects.get(cpf_cnpj="18084974300")
        self.assertEqual(associado_importado.status, Associado.Status.INADIMPLENTE)
        self.assertEqual(associado_importado.arquivo_retorno_origem, "retorno_etipi_052025.txt")
        self.assertEqual(associado_importado.ultimo_arquivo_retorno, "retorno_etipi_052025.txt")
        self.assertEqual(associado_importado.competencia_importacao_retorno, date(2025, 5, 1))

        response = self.coord_client.get(
            f"/api/v1/importacao/arquivo-retorno/{payload['id']}/descontados/",
            {"page_size": 10},
        )
        self.assertEqual(response.status_code, 200, response.json())
        detail_payload = response.json()
        self.assertEqual(detail_payload["count"], 2)
        self.assertEqual(len(detail_payload["results"]), 2)
        self.assertEqual(detail_payload["results"][0]["status_codigo"], "1")
        self.assertEqual(detail_payload["results"][0]["agente_responsavel"], "Tes ABASE")
        self.assertIsNotNone(detail_payload["results"][0]["associado_id"])
        self.assertTrue(detail_payload["results"][0]["associado_matricula"])

    def test_processamento_baixa_parcela_de_inativo_sem_reativar_associado(self):
        associado, _contrato, ciclo = self.create_associado_com_contrato(
            cpf="10000000008",
            nome="Servidor Inativo Processado",
            competencia_final=date(2025, 5, 1),
            status_ultima_parcela=Parcela.Status.EM_ABERTO,
            valor_mensalidade=Decimal("150.00"),
        )
        associado.status = Associado.Status.INATIVO
        associado.save(update_fields=["status", "updated_at"])

        arquivo = self.create_arquivo_retorno(nome="retorno_inativo_processado.txt")
        parsed = SimpleNamespace(
            meta=SimpleNamespace(
                competencia="05/2025",
                data_geracao="22/04/2026",
                entidade="ABASE",
                sistema_origem="ETIPI/iNETConsig",
            ),
            items=[
                {
                    "linha_numero": 1,
                    "cpf_cnpj": associado.cpf_cnpj,
                    "matricula_servidor": "MAT-INATIVO",
                    "nome_servidor": associado.nome_completo,
                    "cargo": "SERVIDOR",
                    "competencia": "05/2025",
                    "valor_descontado": Decimal("150.00"),
                    "status_codigo": "1",
                    "status_desconto": ArquivoRetornoItem.StatusDesconto.EFETIVADO,
                    "status_descricao": "Lançado e Efetivado",
                    "motivo_rejeicao": None,
                    "orgao_codigo": "001",
                    "orgao_pagto_codigo": "001",
                    "orgao_pagto_nome": associado.orgao_publico,
                    "payload_bruto": {},
                }
            ],
            warnings=[],
        )

        service = ArquivoRetornoService()
        with patch.object(service.parser, "parse", return_value=parsed):
            service.processar(arquivo.id)

        associado.refresh_from_db()
        parcela = Parcela.objects.get(ciclo=ciclo, referencia_mes=date(2025, 5, 1))
        item = ArquivoRetornoItem.objects.get(
            arquivo_retorno=arquivo,
            cpf_cnpj=associado.cpf_cnpj,
        )

        self.assertEqual(associado.status, Associado.Status.INATIVO)
        self.assertEqual(parcela.status, Parcela.Status.DESCONTADO)
        self.assertIsNotNone(parcela.data_pagamento)
        self.assertEqual(
            item.resultado_processamento,
            ArquivoRetornoItem.ResultadoProcessamento.BAIXA_EFETUADA,
        )

    def test_list_filtra_por_competencia_e_periodo(self):
        arquivo_jan = self.create_arquivo_retorno(nome="retorno_janeiro.txt")
        arquivo_jan.competencia = date(2026, 1, 1)
        arquivo_jan.status = ArquivoRetorno.Status.CONCLUIDO
        arquivo_jan.save(update_fields=["competencia", "status", "updated_at"])

        arquivo_fev = self.create_arquivo_retorno(nome="retorno_fevereiro.txt")
        arquivo_fev.competencia = date(2026, 2, 1)
        arquivo_fev.status = ArquivoRetorno.Status.CONCLUIDO
        arquivo_fev.save(update_fields=["competencia", "status", "updated_at"])

        arquivo_mar = self.create_arquivo_retorno(nome="retorno_marco.txt")
        arquivo_mar.competencia = date(2026, 3, 1)
        arquivo_mar.status = ArquivoRetorno.Status.CONCLUIDO
        arquivo_mar.save(update_fields=["competencia", "status", "updated_at"])

        response = self.coord_client.get(
            "/api/v1/importacao/arquivo-retorno/",
            {"competencia": "2026-02", "periodo": "mes", "page_size": 10},
        )
        self.assertEqual(response.status_code, 200, response.json())
        payload = response.json()
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["results"][0]["id"], arquivo_fev.id)

        response = self.coord_client.get(
            "/api/v1/importacao/arquivo-retorno/",
            {"competencia": "2026-02", "periodo": "trimestre", "page_size": 10},
        )
        self.assertEqual(response.status_code, 200, response.json())
        payload = response.json()
        self.assertEqual(payload["count"], 3)
        self.assertEqual(
            [row["id"] for row in payload["results"]],
            [arquivo_mar.id, arquivo_fev.id, arquivo_jan.id],
        )

    def test_list_historico_nao_retorna_financeiro_pesado(self):
        arquivo = self.create_arquivo_retorno(nome="retorno_hist.txt")
        arquivo.status = ArquivoRetorno.Status.CONCLUIDO
        arquivo.resultado_resumo = {
            "competencia": "03/2026",
            "financeiro": {
                "esperado": "100.00",
                "recebido": "100.00",
                "ok": 1,
                "total": 1,
                "faltando": 0,
                "pendente": "0.00",
                "percentual": 100.0,
            },
        }
        arquivo.save(update_fields=["status", "resultado_resumo", "updated_at"])

        response = self.coord_client.get(
            "/api/v1/importacao/arquivo-retorno/",
            {"page_size": 10},
        )
        self.assertEqual(response.status_code, 200, response.json())
        payload = response.json()
        self.assertEqual(payload["results"][0]["id"], arquivo.id)
        self.assertEqual(payload["results"][0]["resumo"], {})
        self.assertIsNone(payload["results"][0]["financeiro"])

    def test_processar_reprocessamento_idempotente_nao_duplica_ciclo(self):
        self.create_associado_com_contrato(
            cpf="23993596315",
            nome="Maria de Jesus Santana Costa",
        )
        self.create_associado_com_contrato(
            cpf="21819424391",
            nome="Francisco Crisostomo Batista",
        )
        self.create_associado_com_contrato(
            cpf="48204773315",
            nome="Maria de Jesus Araujo Goncalves",
        )

        service = ArquivoRetornoService()
        arquivo = service.upload(
            SimpleUploadedFile(
                "retorno_etipi_052025.txt",
                self.fixture_bytes(),
                content_type="text/plain",
            ),
            self.tesoureiro,
        )
        arquivo = service.confirmar(arquivo.id)

        contrato = arquivo.itens.get(status_codigo="1").parcela.ciclo.contrato
        self.assertEqual(contrato.ciclos.count(), 1)

        service.processar(arquivo.id)

        contrato.refresh_from_db()
        self.assertEqual(contrato.ciclos.count(), 1)
        self.assertEqual(contrato.ciclos.get(numero=1).parcelas.count(), 3)

    @override_settings(CELERY_TASK_ALWAYS_EAGER=False)
    def test_confirmar_enfileira_sem_processar_inline(self):
        self.create_associado_com_contrato(
            cpf="23993596315",
            nome="Maria de Jesus Santana Costa",
        )
        self.create_associado_com_contrato(
            cpf="21819424391",
            nome="Francisco Crisostomo Batista",
        )
        self.create_associado_com_contrato(
            cpf="48204773315",
            nome="Maria de Jesus Araujo Goncalves",
        )

        service = ArquivoRetornoService()
        arquivo = service.upload(
            SimpleUploadedFile(
                "retorno_etipi_052025.txt",
                self.fixture_bytes(),
                content_type="text/plain",
            ),
            self.coordenador,
        )

        with patch("apps.importacao.tasks.processar_arquivo_retorno.delay") as delay_mock:
            arquivo = service.confirmar(arquivo.id)

        delay_mock.assert_called_once_with(arquivo.id)
        self.assertEqual(arquivo.status, ArquivoRetorno.Status.PENDENTE)
        self.assertEqual(arquivo.itens.count(), 0)
        self.assertNotIn("financeiro", arquivo.resultado_resumo)

    @override_settings(CELERY_TASK_ALWAYS_EAGER=False)
    def test_reprocessar_recupera_processando_antigo(self):
        service = ArquivoRetornoService()
        arquivo = self.create_arquivo_retorno(nome="retorno_preso.txt")
        arquivo.status = ArquivoRetorno.Status.PROCESSANDO
        arquivo.save(update_fields=["status", "updated_at"])
        ArquivoRetorno.objects.filter(pk=arquivo.pk).update(
            updated_at=timezone.now() - timedelta(minutes=15)
        )

        with patch("apps.importacao.tasks.processar_arquivo_retorno.delay") as delay_mock:
            arquivo = service.reprocessar(arquivo.id)

        delay_mock.assert_called_once_with(arquivo.id)
        self.assertEqual(arquivo.status, ArquivoRetorno.Status.PENDENTE)

    def test_ultima_concluida_nao_serializa_dry_run_legado_incompleto(self):
        arquivo = self.create_arquivo_retorno(nome="retorno_legado.txt")
        arquivo.status = ArquivoRetorno.Status.CONCLUIDO
        arquivo.dry_run_resultado = {
            "kpis": {"total_no_arquivo": 1},
            "items": [],
        }
        arquivo.save(update_fields=["status", "dry_run_resultado", "updated_at"])

        response = self.coord_client.get("/api/v1/importacao/arquivo-retorno/ultima/")

        self.assertEqual(response.status_code, 200, response.json())
        self.assertIsNone(response.json()["dry_run_resultado"])

    def test_processar_registra_warning_de_parse_e_continua(self):
        self.create_associado_com_contrato(
            cpf="12345678901",
            nome="Servidor Valido",
        )
        conteudo = """
Entidade: 2102-ABASE                                                 Referência: 05/2025   Data da Geração: 23/05/2025
STATUS MATRICULA NOME                           CARGO                          FIN. ORGAO LANC.  TOTAL PAGO  VALOR        ORGAO PAGTO CPF
====== ========= ============================== ============================== ==== ============ ===== ===== ============ =========== ===========
{linha_valida}
{linha_invalida}
       Órgão Pagamento:  002-SECRETARIA DE TESTE          -  2 Lançamento(s)  -  Total R$ 60.00
""".strip().format(
            linha_valida=build_detail_line(
                "1",
                "RET-1001",
                "SERVIDOR VALIDO",
                "CARGO TESTE",
                "6580",
                "002",
                "001",
                "30.00",
                "30.00",
                "002",
                "12345678901",
            ),
            linha_invalida=build_detail_line(
                "4",
                "RET-1002",
                "SERVIDOR COM ERRO",
                "CARGO TESTE",
                "6580",
                "002",
                "001",
                "30.00",
                "XX.XX",
                "002",
                "12345678902",
            ),
        )

        response = self.coord_client.post(
            "/api/v1/importacao/arquivo-retorno/upload/",
            {
                "arquivo": SimpleUploadedFile(
                    "retorno_warning.txt",
                    conteudo.encode("latin-1"),
                    content_type="text/plain",
                )
            },
            format="multipart",
        )
        self.assertEqual(response.status_code, 201, response.json())

        confirmacao = self.coord_client.post(
            f"/api/v1/importacao/arquivo-retorno/{response.json()['id']}/confirmar/"
        )
        self.assertEqual(confirmacao.status_code, 200, confirmacao.json())

        arquivo = ArquivoRetorno.objects.get(pk=response.json()["id"])
        self.assertEqual(arquivo.total_registros, 1)
        self.assertEqual(arquivo.erros, 1)
        self.assertTrue(
            arquivo.logs.filter(
                tipo=ImportacaoLog.Tipo.PARSE,
                mensagem="Linha malformada ignorada durante o parse.",
            ).exists()
        )

    def test_cancelar_endpoint_e_idempotente_para_preview_ja_removido(self):
        self.create_associado_com_contrato(
            cpf="23993596315",
            nome="Maria de Jesus Santana Costa",
        )

        response = self.coord_client.post(
            "/api/v1/importacao/arquivo-retorno/upload/",
            {
                "arquivo": SimpleUploadedFile(
                    "retorno_etipi_052025.txt",
                    self.fixture_bytes(),
                    content_type="text/plain",
                )
            },
            format="multipart",
        )
        self.assertEqual(response.status_code, 201, response.json())
        arquivo_id = response.json()["id"]

        primeira = self.coord_client.post(
            f"/api/v1/importacao/arquivo-retorno/{arquivo_id}/cancelar/"
        )
        self.assertEqual(primeira.status_code, 204)
        self.assertFalse(ArquivoRetorno.objects.filter(pk=arquivo_id).exists())

        segunda = self.coord_client.post(
            f"/api/v1/importacao/arquivo-retorno/{arquivo_id}/cancelar/"
        )
        self.assertEqual(segunda.status_code, 204)

    def test_processar_consolida_cpf_duplicado_no_padrao_legado(self):
        self.create_associado_com_contrato(
            cpf="12345678901",
            nome="SERVIDOR DUPLICADO",
            matricula_orgao="RET1001",
            orgao_publico="SECRETARIA DE TESTE",
        )
        self.create_associado_com_contrato(
            cpf="98765432100",
            nome="SERVIDOR UNICO",
            matricula_orgao="RET2001",
            orgao_publico="SECRETARIA DE TESTE",
        )
        conteudo = """
Entidade: 2102-ABASE                                                 Referência: 05/2025   Data da Geração: 23/05/2025
STATUS MATRICULA NOME                           CARGO                          FIN. ORGAO LANC.  TOTAL PAGO  VALOR        ORGAO PAGTO CPF
====== ========= ============================== ============================== ==== ============ ===== ===== ============ =========== ===========
{linha_duplicada_1}
{linha_duplicada_2}
       Órgão Pagamento:  002-SECRETARIA DE TESTE          -  2 Lançamento(s)  -  Total R$ 60.00
              Total do Status:  1  -  2 Lançamento(s)  -  Total R$ 60.00
{linha_unica}
       Órgão Pagamento:  002-SECRETARIA DE TESTE          -  1 Lançamento(s)  -  Total R$ 30.00
""".strip().format(
            linha_duplicada_1=build_detail_line(
                "1",
                "RET1001",
                "SERVIDOR DUPLICADO",
                "CARGO TESTE",
                "6580",
                "002",
                "001",
                "30.00",
                "30.00",
                "002",
                "12345678901",
            ),
            linha_duplicada_2=build_detail_line(
                "1",
                "RET1001",
                "SERVIDOR DUPLICADO",
                "CARGO TESTE",
                "6580",
                "002",
                "001",
                "30.00",
                "30.00",
                "002",
                "12345678901",
            ),
            linha_unica=build_detail_line(
                "1",
                "RET2001",
                "SERVIDOR UNICO",
                "CARGO TESTE",
                "6580",
                "002",
                "001",
                "30.00",
                "30.00",
                "002",
                "98765432100",
            ),
        )

        response = self.coord_client.post(
            "/api/v1/importacao/arquivo-retorno/upload/",
            {
                "arquivo": SimpleUploadedFile(
                    "retorno_cpf_duplicado.txt",
                    conteudo.encode("latin-1"),
                    content_type="text/plain",
                )
            },
            format="multipart",
        )
        self.assertEqual(response.status_code, 201, response.json())

        confirmacao = self.coord_client.post(
            f"/api/v1/importacao/arquivo-retorno/{response.json()['id']}/confirmar/"
        )
        self.assertEqual(confirmacao.status_code, 200, confirmacao.json())

        arquivo = ArquivoRetorno.objects.get(pk=response.json()["id"])
        self.assertEqual(arquivo.resultado_resumo["cpfs_duplicados_arquivo"], 1)
        self.assertEqual(arquivo.resultado_resumo["linhas_duplicadas_ignoradas"], 1)
        self.assertEqual(arquivo.resultado_resumo["pendencias_manuais"], 0)
        self.assertEqual(arquivo.resultado_resumo["baixa_efetuada"], 2)
        self.assertEqual(arquivo.resultado_resumo["pm_criados"], 2)
        self.assertEqual(arquivo.resultado_resumo["associados_importados"], 0)

        itens_duplicados = arquivo.itens.filter(cpf_cnpj="12345678901").order_by("linha_numero")
        self.assertEqual(itens_duplicados.count(), 1)
        self.assertEqual(
            itens_duplicados.first().resultado_processamento,
            ArquivoRetornoItem.ResultadoProcessamento.BAIXA_EFETUADA,
        )
        self.assertEqual(
            arquivo.logs.filter(
                tipo=ImportacaoLog.Tipo.VALIDACAO,
                mensagem="CPF duplicado consolidado no padrão legado.",
            ).count(),
            1,
        )

    def test_upload_outubro_replica_totais_do_legado(self):
        response = self.coord_client.post(
            "/api/v1/importacao/arquivo-retorno/upload/",
            {
                "arquivo": SimpleUploadedFile(
                    "Relatorio_D2102-10-2025_inicio.txt",
                    self.fixture_bytes("retorno_etipi_102025.txt"),
                    content_type="text/plain",
                )
            },
            format="multipart",
        )
        self.assertEqual(response.status_code, 201, response.json())

        confirmacao = self.coord_client.post(
            f"/api/v1/importacao/arquivo-retorno/{response.json()['id']}/confirmar/"
        )
        self.assertEqual(confirmacao.status_code, 200, confirmacao.json())

        arquivo = ArquivoRetorno.objects.get(pk=response.json()["id"])
        self.assertEqual(arquivo.total_registros, 239)
        self.assertEqual(arquivo.resultado_resumo["cpfs_duplicados_arquivo"], 1)
        self.assertEqual(arquivo.resultado_resumo["linhas_duplicadas_ignoradas"], 0)

        pagamentos = PagamentoMensalidade.objects.filter(referencia_month=date(2025, 10, 1))
        self.assertEqual(pagamentos.count(), 239)
        self.assertEqual(
            pagamentos.aggregate(total=Sum("valor"))["total"],
            Decimal("47961.38"),
        )
        self.assertEqual(pagamentos.filter(status_code__in=["1", "4"]).count(), 217)
        self.assertEqual(
            pagamentos.filter(status_code__in=["1", "4"]).aggregate(total=Sum("valor"))["total"],
            Decimal("45491.38"),
        )

        alceanira = pagamentos.filter(cpf_cnpj="67556906353")
        self.assertEqual(alceanira.count(), 2)
        self.assertEqual(
            alceanira.aggregate(total=Sum("valor"))["total"],
            Decimal("60.00"),
        )

        financeiro = self.coord_client.get(
            f"/api/v1/importacao/arquivo-retorno/{arquivo.id}/financeiro/"
        )
        self.assertEqual(financeiro.status_code, 200, financeiro.json())
        payload = financeiro.json()
        self.assertEqual(payload["resumo"]["total"], 239)
        self.assertEqual(payload["resumo"]["esperado"], "47961.38")
        self.assertEqual(payload["resumo"]["ok"], 217)
        self.assertEqual(payload["resumo"]["recebido"], "45491.38")
        self.assertEqual(len(payload["rows"]), 239)

    def test_build_financeiro_payload_serializa_valores_monetarios_como_string(self):
        self.create_associado_com_contrato(
            cpf="23993596315",
            nome="Maria de Jesus Santana Costa",
        )
        self.create_associado_com_contrato(
            cpf="21819424391",
            nome="Francisco Crisostomo Batista",
        )
        self.create_associado_com_contrato(
            cpf="48204773315",
            nome="Maria de Jesus Araujo Goncalves",
        )

        response = self.coord_client.post(
            "/api/v1/importacao/arquivo-retorno/upload/",
            {
                "arquivo": SimpleUploadedFile(
                    "retorno_etipi_052025.txt",
                    self.fixture_bytes(),
                    content_type="text/plain",
                )
            },
            format="multipart",
        )
        self.assertEqual(response.status_code, 201, response.json())

        confirmacao = self.coord_client.post(
            f"/api/v1/importacao/arquivo-retorno/{response.json()['id']}/confirmar/"
        )
        self.assertEqual(confirmacao.status_code, 200, confirmacao.json())

        payload = build_financeiro_payload(competencia=date(2025, 5, 1))
        self.assertIsInstance(payload["resumo"]["esperado"], str)
        self.assertIsInstance(payload["resumo"]["recebido"], str)
        self.assertEqual(payload["resumo"]["esperado"], "120.00")
        self.assertEqual(payload["resumo"]["recebido"], "60.00")
        self.assertTrue(payload["rows"])
        self.assertIsInstance(payload["rows"][0]["valor"], str)
        self.assertIsInstance(payload["rows"][0]["esperado"], str)
        self.assertIsInstance(payload["rows"][0]["recebido"], str)

    def test_upload_preenche_dry_run_com_associados_importados(self):
        self.create_associado_com_contrato(
            cpf="23993596315",
            nome="Maria de Jesus Santana Costa",
        )
        self.create_associado_com_contrato(
            cpf="21819424391",
            nome="Francisco Crisostomo Batista",
        )
        self.create_associado_com_contrato(
            cpf="48204773315",
            nome="Maria de Jesus Araujo Goncalves",
        )

        service = ArquivoRetornoService()
        arquivo = service.upload(
            SimpleUploadedFile(
                "retorno_etipi_052025.txt",
                self.fixture_bytes(),
                content_type="text/plain",
            ),
            self.coordenador,
        )

        self.assertEqual(arquivo.status, ArquivoRetorno.Status.AGUARDANDO_CONFIRMACAO)
        self.assertEqual(arquivo.dry_run_resultado["kpis"]["associados_importados"], 1)
        item_importado = next(
            item
            for item in arquivo.dry_run_resultado["items"]
            if item["associado_importado"]
        )
        self.assertEqual(item_importado["resultado"], "nao_descontado")
        self.assertEqual(item_importado["associado_status_depois"], Associado.Status.INADIMPLENTE)

    def test_importacao_futura_atualiza_mesmo_associado_importado(self):
        service = ArquivoRetornoService()
        arquivo_inicial = self.create_arquivo_retorno(nome="retorno_inicial.txt")
        arquivo_inicial.resultado_resumo = {
            "competencia": "05/2025",
            "data_geracao": "23/05/2025",
        }
        arquivo_inicial.save(update_fields=["resultado_resumo", "updated_at"])

        resumo_um = service._upsert_pagamentos_mensalidade(
            arquivo_retorno=arquivo_inicial,
            items=[
                {
                    "linha_numero": 1,
                    "cpf_cnpj": "18084974300",
                    "matricula_servidor": "021293-8",
                    "nome_servidor": "MIGUEL ALVES DO NASCIMENTO",
                    "cargo": "AGENTE OPERACIONAL",
                    "orgao_pagto_nome": "SEAD",
                    "status_codigo": "3",
                    "valor_descontado": "30.00",
                }
            ],
            import_uuid="import-1",
            user=self.coordenador,
        )
        associado = Associado.objects.get(cpf_cnpj="18084974300")

        arquivo_seguinte = self.create_arquivo_retorno(nome="retorno_seguinte.txt")
        arquivo_seguinte.competencia = date(2025, 6, 1)
        arquivo_seguinte.resultado_resumo = {
            "competencia": "06/2025",
            "data_geracao": "21/06/2025",
        }
        arquivo_seguinte.save(
            update_fields=["competencia", "resultado_resumo", "updated_at"]
        )

        resumo_dois = service._upsert_pagamentos_mensalidade(
            arquivo_retorno=arquivo_seguinte,
            items=[
                {
                    "linha_numero": 1,
                    "cpf_cnpj": "18084974300",
                    "matricula_servidor": "021293-9",
                    "nome_servidor": "MIGUEL ALVES NASCIMENTO",
                    "cargo": "AGENTE OPERACIONAL SR",
                    "orgao_pagto_nome": "SEAD NOVA",
                    "status_codigo": "1",
                    "valor_descontado": "30.00",
                }
            ],
            import_uuid="import-2",
            user=self.coordenador,
        )

        associado.refresh_from_db()
        self.assertEqual(Associado.objects.filter(cpf_cnpj="18084974300").count(), 1)
        self.assertEqual(associado.id, Associado.objects.get(cpf_cnpj="18084974300").id)
        self.assertEqual(associado.status, Associado.Status.IMPORTADO)
        self.assertEqual(associado.nome_completo, "MIGUEL ALVES NASCIMENTO")
        self.assertEqual(associado.matricula_orgao, "021293-9")
        self.assertEqual(associado.orgao_publico, "SEAD NOVA")
        self.assertEqual(associado.cargo, "AGENTE OPERACIONAL SR")
        self.assertEqual(associado.arquivo_retorno_origem, "retorno_inicial.txt")
        self.assertEqual(associado.ultimo_arquivo_retorno, "retorno_seguinte.txt")
        self.assertEqual(associado.competencia_importacao_retorno, date(2025, 5, 1))
        self.assertEqual(resumo_um["pm_associados_importados"], 1)
        self.assertEqual(resumo_dois["pm_associados_importados"], 0)

    def test_reimportacao_mesmo_mes_cria_novo_pagamento_quando_matricula_diverge(self):
        associado, _contrato, _ciclo = self.create_associado_com_contrato(
            cpf="18084974300",
            nome="MIGUEL ALVES NASCIMENTO",
        )
        pagamento = PagamentoMensalidade.objects.create(
            created_by=self.coordenador,
            import_uuid="import-antigo",
            referencia_month=date(2025, 12, 1),
            status_code="2",
            matricula="021293-8",
            orgao_pagto="SEAD ANTIGA",
            nome_relatorio="MIGUEL ALVES DO NASCIMENTO",
            cpf_cnpj=associado.cpf_cnpj,
            associado=None,
            valor=Decimal("30.00"),
            source_file_path="arquivos_retorno/retorno_antigo.txt",
        )

        arquivo = self.create_arquivo_retorno(nome="retorno_dezembro_corrigido.txt")
        arquivo.competencia = date(2025, 12, 1)
        arquivo.resultado_resumo = {
            "competencia": "12/2025",
            "data_geracao": "26/12/2025",
        }
        arquivo.save(update_fields=["competencia", "resultado_resumo", "updated_at"])

        service = ArquivoRetornoService()
        resumo = service._upsert_pagamentos_mensalidade(
            arquivo_retorno=arquivo,
            items=[
                {
                    "linha_numero": 1,
                    "cpf_cnpj": associado.cpf_cnpj,
                    "matricula_servidor": "021293-9",
                    "nome_servidor": "MIGUEL ALVES NASCIMENTO",
                    "cargo": "AGENTE OPERACIONAL SR",
                    "orgao_pagto_nome": "SEAD NOVA",
                    "status_codigo": "1",
                    "valor_descontado": "45.00",
                }
            ],
            import_uuid="import-corrigido",
            user=self.coordenador,
        )

        pagamento.refresh_from_db()
        self.assertEqual(
            PagamentoMensalidade.objects.filter(
                cpf_cnpj=associado.cpf_cnpj,
                referencia_month=date(2025, 12, 1),
            ).count(),
            2,
        )
        self.assertEqual(pagamento.status_code, "2")
        self.assertEqual(pagamento.matricula, "021293-8")
        self.assertEqual(pagamento.orgao_pagto, "SEAD ANTIGA")
        self.assertEqual(pagamento.nome_relatorio, "MIGUEL ALVES DO NASCIMENTO")
        self.assertEqual(pagamento.valor, Decimal("30.00"))
        self.assertEqual(pagamento.import_uuid, "import-antigo")
        self.assertEqual(pagamento.source_file_path, "arquivos_retorno/retorno_antigo.txt")
        self.assertIsNone(pagamento.associado_id)

        pagamento_novo = PagamentoMensalidade.objects.get(
            cpf_cnpj=associado.cpf_cnpj,
            referencia_month=date(2025, 12, 1),
            matricula="021293-9",
        )
        self.assertEqual(pagamento_novo.status_code, "1")
        self.assertEqual(pagamento_novo.orgao_pagto, "SEAD NOVA")
        self.assertEqual(pagamento_novo.nome_relatorio, "MIGUEL ALVES NASCIMENTO")
        self.assertEqual(pagamento_novo.valor, Decimal("45.00"))
        self.assertEqual(pagamento_novo.import_uuid, "import-corrigido")
        self.assertEqual(pagamento_novo.source_file_path, arquivo.arquivo_url)
        self.assertEqual(pagamento_novo.associado_id, associado.id)
        self.assertEqual(resumo["pm_criados"], 1)
        self.assertEqual(resumo["pm_duplicados"], 0)
        self.assertEqual(resumo["pm_vinculados"], 1)

    def test_reimportacao_mesmo_mes_promove_baixa_manual_legada_para_retorno_efetivado(self):
        associado, _contrato, _ciclo = self.create_associado_com_contrato(
            cpf="77852621368",
            nome="ACACIO LUSTOSA DANTAS",
            matricula_orgao="362602-4",
            orgao_publico="SECRETARIA DA EDUCACAO",
        )
        pagamento = PagamentoMensalidade.objects.create(
            created_by=self.coordenador,
            import_uuid="manual-fev",
            referencia_month=date(2026, 2, 1),
            status_code="M",
            matricula="362602-4",
            orgao_pagto="918",
            nome_relatorio="ACACIO LUSTOSA DANTAS",
            cpf_cnpj=associado.cpf_cnpj,
            associado=associado,
            valor=Decimal("100.00"),
            manual_status=PagamentoMensalidade.ManualStatus.PAGO,
            manual_paid_at=timezone.make_aware(datetime(2026, 2, 5, 0, 0, 0)),
            recebido_manual=Decimal("100.00"),
            source_file_path="tesouraria/baixa_manual_fev.pdf",
        )

        arquivo = self.create_arquivo_retorno(nome="retorno_fev_corrigido.txt")
        arquivo.competencia = date(2026, 2, 1)
        arquivo.resultado_resumo = {
            "competencia": "02/2026",
            "data_geracao": "04/03/2026",
        }
        arquivo.save(update_fields=["competencia", "resultado_resumo", "updated_at"])

        service = ArquivoRetornoService()
        resumo = service._upsert_pagamentos_mensalidade(
            arquivo_retorno=arquivo,
            items=[
                {
                    "linha_numero": 710,
                    "cpf_cnpj": associado.cpf_cnpj,
                    "matricula_servidor": "362602-4",
                    "nome_servidor": "ACACIO LUSTOSA DANTAS",
                    "cargo": "-SEM PLANO",
                    "orgao_pagto_nome": "SECRETARIA DA EDUCACAO",
                    "status_codigo": "1",
                    "valor_descontado": "100.00",
                }
            ],
            import_uuid="import-fev-retorno",
            user=self.coordenador,
        )

        pagamento.refresh_from_db()
        self.assertEqual(pagamento.status_code, "1")
        self.assertIsNone(pagamento.manual_status)
        self.assertIsNone(pagamento.manual_paid_at)
        self.assertIsNone(pagamento.recebido_manual)
        self.assertEqual(pagamento.import_uuid, "import-fev-retorno")
        self.assertEqual(pagamento.source_file_path, arquivo.arquivo_url)
        self.assertEqual(resumo["pm_criados"], 0)
        self.assertEqual(resumo["pm_duplicados"], 1)
        self.assertEqual(resumo["pm_duplicidades_abertas"], 0)
        self.assertEqual(DuplicidadeFinanceira.objects.count(), 0)

    def test_reimportacao_mesmo_mes_sobrescreve_snapshot_legado_quando_retorno_rejeita(self):
        associado, _contrato, _ciclo = self.create_associado_com_contrato(
            cpf="77852621369",
            nome="ACACIO LUSTOSA RETORNO REJEITADO",
            matricula_orgao="362602-5",
            orgao_publico="SECRETARIA DA EDUCACAO",
        )
        pagamento = PagamentoMensalidade.objects.create(
            created_by=self.coordenador,
            import_uuid="manual-fev",
            referencia_month=date(2026, 2, 1),
            status_code="M",
            matricula="362602-5",
            orgao_pagto="918",
            nome_relatorio="ACACIO LUSTOSA RETORNO REJEITADO",
            cpf_cnpj=associado.cpf_cnpj,
            associado=associado,
            valor=Decimal("100.00"),
            manual_status=PagamentoMensalidade.ManualStatus.PAGO,
            manual_paid_at=timezone.make_aware(datetime(2026, 2, 5, 0, 0, 0)),
            recebido_manual=Decimal("100.00"),
            source_file_path="legacy/pagamentos_mensalidades",
        )

        arquivo = self.create_arquivo_retorno(nome="retorno_fev_rejeitado.txt")
        arquivo.competencia = date(2026, 2, 1)
        arquivo.resultado_resumo = {
            "competencia": "02/2026",
            "data_geracao": "04/03/2026",
        }
        arquivo.save(update_fields=["competencia", "resultado_resumo", "updated_at"])

        service = ArquivoRetornoService()
        resumo = service._upsert_pagamentos_mensalidade(
            arquivo_retorno=arquivo,
            items=[
                {
                    "linha_numero": 711,
                    "cpf_cnpj": associado.cpf_cnpj,
                    "matricula_servidor": "362602-5",
                    "nome_servidor": "ACACIO LUSTOSA RETORNO REJEITADO",
                    "cargo": "-SEM PLANO",
                    "orgao_pagto_nome": "SECRETARIA DA EDUCACAO",
                    "status_codigo": "2",
                    "valor_descontado": "100.00",
                }
            ],
            import_uuid="import-fev-retorno-rejeitado",
            user=self.coordenador,
        )

        pagamento.refresh_from_db()
        self.assertEqual(pagamento.status_code, "2")
        self.assertIsNone(pagamento.manual_status)
        self.assertIsNone(pagamento.manual_paid_at)
        self.assertIsNone(pagamento.recebido_manual)
        self.assertEqual(pagamento.import_uuid, "import-fev-retorno-rejeitado")
        self.assertEqual(pagamento.source_file_path, arquivo.arquivo_url)
        self.assertEqual(resumo["pm_criados"], 0)
        self.assertEqual(resumo["pm_duplicados"], 1)
        self.assertEqual(resumo["pm_duplicidades_abertas"], 0)
        self.assertEqual(DuplicidadeFinanceira.objects.count(), 0)

    def test_upload_retorno_oficial_nao_reaplica_snapshot_legado_quando_ha_status_explicito(self):
        self.create_associado_com_contrato(
            cpf="23993596315",
            nome="Maria de Jesus Santana Costa",
        )
        self.create_associado_com_contrato(
            cpf="21819424391",
            nome="Francisco Crisostomo Batista",
        )
        self.create_associado_com_contrato(
            cpf="48204773315",
            nome="Maria de Jesus Araujo Goncalves",
        )

        with patch(
            "apps.importacao.services.list_legacy_pagamento_snapshots",
            return_value={
                "21819424391": LegacyPagamentoSnapshot(
                    manual_status="pago",
                    esperado_manual=Decimal("30.00"),
                    recebido_manual=Decimal("30.00"),
                    manual_forma_pagamento="PIX",
                    agente_refi_solicitado=True,
                )
            },
        ):
            response = self.coord_client.post(
                "/api/v1/importacao/arquivo-retorno/upload/",
                {
                    "arquivo": SimpleUploadedFile(
                        "retorno_etipi_052025.txt",
                        self.fixture_bytes(),
                        content_type="text/plain",
                    )
                },
                format="multipart",
            )
            self.assertEqual(response.status_code, 201, response.json())

            confirmacao = self.coord_client.post(
                f"/api/v1/importacao/arquivo-retorno/{response.json()['id']}/confirmar/"
            )
            self.assertEqual(confirmacao.status_code, 200, confirmacao.json())

            pagamento = PagamentoMensalidade.objects.get(
                cpf_cnpj="21819424391",
                referencia_month=date(2025, 5, 1),
            )
            self.assertEqual(pagamento.status_code, "2")
            self.assertIsNone(pagamento.manual_status)
            self.assertIsNone(pagamento.recebido_manual)
            self.assertEqual(pagamento.manual_forma_pagamento, "")
            self.assertFalse(pagamento.agente_refi_solicitado)

            financeiro = self.coord_client.get(
                f"/api/v1/importacao/arquivo-retorno/{response.json()['id']}/financeiro/"
            )
            self.assertEqual(financeiro.status_code, 200, financeiro.json())
            payload = financeiro.json()
            self.assertEqual(payload["resumo"]["ok"], 2)
            self.assertEqual(payload["resumo"]["recebido"], "60.00")

    def test_processar_envia_conflito_manual_para_esteira_de_duplicidade_quando_valor_diverge(self):
        associado = Associado.objects.create(
            nome_completo="ACACIO LUSTOSA DANTAS",
            cpf_cnpj="77852621368",
            email="acacio@teste.local",
            telefone="86999999999",
            orgao_publico="SECRETARIA DA EDUCACAO",
            matricula_orgao="362602-4",
            status=Associado.Status.ATIVO,
            agente_responsavel=self.tesoureiro,
        )
        contrato = Contrato.objects.create(
            associado=associado,
            agente=self.tesoureiro,
            codigo="CTR-TESTE-ACACIO",
            valor_bruto=Decimal("300.00"),
            valor_liquido=Decimal("210.00"),
            valor_mensalidade=Decimal("100.00"),
            prazo_meses=3,
            taxa_antecipacao=Decimal("30.00"),
            margem_disponivel=Decimal("210.00"),
            valor_total_antecipacao=Decimal("300.00"),
            comissao_agente=Decimal("21.00"),
            status=Contrato.Status.ATIVO,
            data_contrato=date(2025, 11, 25),
            data_aprovacao=date(2025, 11, 25),
            data_primeira_mensalidade=date(2026, 1, 7),
            mes_averbacao=date(2025, 12, 1),
            auxilio_liberado_em=date(2025, 11, 25),
        )
        PagamentoMensalidade.objects.create(
            created_by=self.tesoureiro,
            import_uuid="import-dez",
            referencia_month=date(2025, 12, 1),
            status_code="1",
            matricula="362602-4",
            orgao_pagto="918",
            nome_relatorio="ACACIO LUSTOSA DANTAS",
            cpf_cnpj=associado.cpf_cnpj,
            associado=associado,
            valor=Decimal("100.00"),
            source_file_path="retornos/dezembro.txt",
        )
        PagamentoMensalidade.objects.create(
            created_by=self.tesoureiro,
            import_uuid="import-jan",
            referencia_month=date(2026, 1, 1),
            status_code="1",
            matricula="362602-4",
            orgao_pagto="918",
            nome_relatorio="ACACIO LUSTOSA DANTAS",
            cpf_cnpj=associado.cpf_cnpj,
            associado=associado,
            valor=Decimal("100.00"),
            source_file_path="retornos/janeiro.txt",
        )
        PagamentoMensalidade.objects.create(
            created_by=self.tesoureiro,
            import_uuid="manual-fev",
            referencia_month=date(2026, 2, 1),
            status_code="M",
            matricula="362602-4",
            orgao_pagto="918",
            nome_relatorio="ACACIO LUSTOSA DANTAS",
            cpf_cnpj=associado.cpf_cnpj,
            associado=associado,
            valor=Decimal("100.00"),
            manual_status=PagamentoMensalidade.ManualStatus.PAGO,
            manual_paid_at=timezone.make_aware(datetime(2026, 2, 5, 0, 0, 0)),
            recebido_manual=Decimal("100.00"),
            source_file_path="tesouraria/baixa_manual_fev.pdf",
        )

        with patch(
            "apps.contratos.cycle_projection.timezone.localdate",
            return_value=date(2026, 3, 21),
        ):
            rebuild_contract_cycle_state(contrato, execute=True)

        self.assertEqual(
            list(
                Parcela.objects.filter(ciclo__contrato=contrato)
                .order_by("ciclo__numero", "numero")
                .values_list("referencia_mes", flat=True)
            ),
            [date(2025, 12, 1), date(2026, 1, 1), date(2026, 3, 1)],
        )

        arquivo = self.create_arquivo_retorno(nome="retorno_fev_acacio.txt")
        arquivo.competencia = date(2026, 2, 1)
        arquivo.resultado_resumo = {"competencia": "02/2026"}
        arquivo.save(update_fields=["competencia", "resultado_resumo", "updated_at"])
        default_storage.save(arquivo.arquivo_url, ContentFile(b"retorno-fevereiro"))

        parsed = SimpleNamespace(
            meta=SimpleNamespace(
                competencia="02/2026",
                data_geracao="04/03/2026",
                entidade="2102-ABASE",
                sistema_origem="ETIPI/iNETConsig",
            ),
            items=[
                {
                    "linha_numero": 710,
                    "cpf_cnpj": associado.cpf_cnpj,
                    "matricula_servidor": "362602-4",
                    "nome_servidor": "ACACIO LUSTOSA DANTAS",
                    "cargo": "-SEM PLANO",
                    "competencia": "02/2026",
                    "valor_descontado": Decimal("120.00"),
                    "status_codigo": "1",
                    "status_desconto": ArquivoRetornoItem.StatusDesconto.EFETIVADO,
                    "status_descricao": "Lançado e Efetivado",
                    "motivo_rejeicao": None,
                    "orgao_codigo": "6580",
                    "orgao_pagto_codigo": "918",
                    "orgao_pagto_nome": "SECRETARIA DA EDUCACAO",
                    "payload_bruto": {},
                }
            ],
            warnings=[],
        )

        service = ArquivoRetornoService()
        with (
            patch.object(service.parser, "parse", return_value=parsed),
            patch(
                "apps.contratos.cycle_projection.timezone.localdate",
                return_value=date(2026, 3, 21),
            ),
            patch(
                "apps.importacao.reconciliacao.timezone.localdate",
                return_value=date(2026, 3, 21),
            ),
        ):
            service.processar(arquivo.id)

        pagamento = PagamentoMensalidade.objects.get(
            cpf_cnpj=associado.cpf_cnpj,
            referencia_month=date(2026, 2, 1),
        )
        item = ArquivoRetornoItem.objects.get(
            arquivo_retorno=arquivo,
            cpf_cnpj=associado.cpf_cnpj,
        )
        contrato.refresh_from_db()

        self.assertEqual(pagamento.status_code, "1")
        self.assertIsNone(pagamento.manual_status)
        self.assertIsNone(pagamento.manual_paid_at)
        self.assertIsNone(pagamento.recebido_manual)
        self.assertEqual(pagamento.source_file_path, arquivo.arquivo_url)
        self.assertEqual(
            item.resultado_processamento,
            ArquivoRetornoItem.ResultadoProcessamento.BAIXA_EFETUADA,
        )
        self.assertIsNone(item.parcela_id)
        self.assertEqual(item.associado_id, associado.id)
        self.assertIn("sem parcela local", item.observacao.lower())
        self.assertEqual(DuplicidadeFinanceira.objects.count(), 0)
        self.assertEqual(contrato.ciclos.count(), 1)
        self.assertEqual(
            list(
                Parcela.objects.filter(ciclo__contrato=contrato)
                .order_by("ciclo__numero", "numero")
                .values_list("referencia_mes", flat=True)
            ),
            [date(2025, 12, 1), date(2026, 1, 1), date(2026, 3, 1)],
        )

        response = self.coord_client.get(
            "/api/v1/importacao/duplicidades-financeiras/",
            {"page_size": 10},
        )
        self.assertEqual(response.status_code, 200, response.json())
        self.assertEqual(response.json()["count"], 0)
        self.assertEqual(response.json()["kpis"]["abertas"], 0)

        sidebar_response = self.coord_client.get(
            "/api/v1/importacao/duplicidades-financeiras/",
            {"status": "aberta", "summary_only": "1"},
        )
        self.assertEqual(sidebar_response.status_code, 200, sidebar_response.json())
        self.assertEqual(sidebar_response.json()["count"], 0)
        self.assertEqual(sidebar_response.json()["kpis"]["abertas"], 0)
        self.assertEqual(sidebar_response.json()["results"], [])

    def test_resolver_duplicidade_com_devolucao(self):
        associado, contrato, _ciclo = self.create_associado_com_contrato(
            cpf="99988877766",
            nome="Associado Duplicidade",
            matricula_orgao="MAT-DUP",
        )
        pagamento = PagamentoMensalidade.objects.create(
            created_by=self.tesoureiro,
            import_uuid="manual-mar",
            referencia_month=date(2025, 5, 1),
            status_code="M",
            matricula="MAT-DUP",
            orgao_pagto="918",
            nome_relatorio=associado.nome_completo,
            cpf_cnpj=associado.cpf_cnpj,
            associado=associado,
            valor=Decimal("30.00"),
            manual_status=PagamentoMensalidade.ManualStatus.PAGO,
            manual_paid_at=timezone.now(),
            recebido_manual=Decimal("30.00"),
            source_file_path="tesouraria/baixa_manual_mar.pdf",
        )
        arquivo = self.create_arquivo_retorno(nome="retorno_dup.txt")
        item = ArquivoRetornoItem.objects.create(
            arquivo_retorno=arquivo,
            linha_numero=10,
            cpf_cnpj=associado.cpf_cnpj,
            matricula_servidor="MAT-DUP",
            nome_servidor=associado.nome_completo,
            cargo="Cargo",
            competencia="05/2025",
            valor_descontado=Decimal("30.00"),
            status_codigo="1",
            status_desconto=ArquivoRetornoItem.StatusDesconto.EFETIVADO,
            status_descricao="Efetivado",
        )
        duplicidade = DuplicidadeFinanceira.objects.create(
            arquivo_retorno_item=item,
            pagamento_mensalidade=pagamento,
            associado=associado,
            contrato=contrato,
            motivo=DuplicidadeFinanceira.Motivo.BAIXA_MANUAL_DUPLICADA,
            competencia_retorno=date(2025, 5, 1),
            competencia_manual=date(2025, 5, 1),
            valor_retorno=Decimal("30.00"),
            valor_manual=Decimal("30.00"),
        )

        response = self.coord_client.post(
            f"/api/v1/importacao/duplicidades-financeiras/{duplicidade.id}/resolver-devolucao/",
            {
                "data_devolucao": "2025-05-20",
                "valor": "30.00",
                "motivo": "Desconto duplicado confirmado.",
                "comprovantes": [
                    SimpleUploadedFile(
                        "duplicidade.pdf",
                        b"arquivo",
                        content_type="application/pdf",
                    )
                ],
            },
        )
        self.assertEqual(response.status_code, 200, response.json())

        duplicidade.refresh_from_db()
        self.assertEqual(duplicidade.status, DuplicidadeFinanceira.Status.RESOLVIDA)
        self.assertIsNotNone(duplicidade.devolucao_id)
        self.assertTrue(
            DevolucaoAssociado.objects.filter(
                pk=duplicidade.devolucao_id,
                tipo=DevolucaoAssociado.Tipo.DESCONTO_INDEVIDO,
            ).exists()
        )
