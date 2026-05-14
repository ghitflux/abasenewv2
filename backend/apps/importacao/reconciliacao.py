from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.associados.models import Associado, only_digits
from apps.contratos.competencia import propagate_competencia_status, resolve_processing_competencia_parcela
from apps.contratos.cycle_projection import (
    invalidate_operational_apt_queue_cache,
    sync_associado_mother_status,
)
from apps.contratos.models import Parcela

from .matching import find_associado
from .models import ArquivoRetorno, ArquivoRetornoItem, ImportacaoLog
from .return_auto_enrollment import (
    is_synthetic_return_contract_code,
    resolve_or_create_imported_associado,
    should_align_parcela_value_from_return,
)


def parse_competencia(value: str) -> date:
    return datetime.strptime(value, "%m/%Y").date().replace(day=1)


class MotorReconciliacao:
    def __init__(self, arquivo_retorno: ArquivoRetorno):
        self.arquivo_retorno = arquivo_retorno
        self.today = timezone.localdate()
        self._associados_para_sync: dict[int, "Associado"] = {}

    def reconciliar(self) -> dict[str, int]:
        resumo = {
            "baixa_efetuada": 0,
            "nao_descontado": 0,
            "pendencias_manuais": 0,
            "duplicidades": 0,
            "nao_encontrado": 0,
            "associados_importados": 0,
            "erro": 0,
            "ciclo_aberto": 0,
            "encerramentos": 0,
            "novos_ciclos": 0,
            "efetivados": 0,
            "nao_descontados": 0,
        }

        itens = (
            self.arquivo_retorno.itens.select_related("associado", "parcela")
            .order_by("linha_numero")
        )
        for item in itens:
            if item.processado:
                outcome = self._outcome_from_item(item)
            else:
                outcome = self.reconciliar_item(item)
            resultado = outcome["resultado"]
            if resultado == ArquivoRetornoItem.ResultadoProcessamento.PENDENCIA_MANUAL:
                resumo["pendencias_manuais"] += 1
            elif resultado == ArquivoRetornoItem.ResultadoProcessamento.DUPLICIDADE:
                resumo["duplicidades"] += 1
            else:
                resumo[resultado] += 1
            if outcome["resultado"] == ArquivoRetornoItem.ResultadoProcessamento.BAIXA_EFETUADA:
                resumo["efetivados"] += 1
            if outcome["resultado"] == ArquivoRetornoItem.ResultadoProcessamento.NAO_DESCONTADO:
                resumo["nao_descontados"] += 1
            if outcome.get("associado_importado"):
                resumo["associados_importados"] += 1
            if outcome["gerou_encerramento"]:
                resumo["encerramentos"] += 1
            if outcome["gerou_novo_ciclo"]:
                resumo["novos_ciclos"] += 1

        # Sync de status dos associados em lote, após todos os itens processados.
        # Evita chamar build_contract_cycle_projection N vezes dentro de transações individuais.
        if self._associados_para_sync:
            invalidate_operational_apt_queue_cache()
            for associado in self._associados_para_sync.values():
                sync_associado_mother_status(
                    associado,
                    invalidate_queue_cache=False,
                )

        return resumo

    def _outcome_from_item(self, item: ArquivoRetornoItem) -> dict[str, object]:
        return {
            "resultado": item.resultado_processamento,
            "gerou_encerramento": item.gerou_encerramento,
            "gerou_novo_ciclo": item.gerou_novo_ciclo,
            "associado_importado": False,
        }

    @transaction.atomic
    def reconciliar_item(self, item: ArquivoRetornoItem) -> dict[str, object]:
        cpf = only_digits(item.cpf_cnpj)
        competencia = parse_competencia(item.competencia)
        associated_imported = False
        associado = item.associado or find_associado(
            cpf=cpf,
            matricula=item.matricula_servidor,
            nome=item.nome_servidor,
            orgao=item.orgao_pagto_nome,
            orgao_alternativo=item.orgao_pagto_codigo,
            orgao_codigo=item.orgao_codigo,
        )
        if (
            associado is not None
            and associado.status == Associado.Status.IMPORTADO
            and associado.ultimo_arquivo_retorno == self.arquivo_retorno.arquivo_nome
            and associado.competencia_importacao_retorno == competencia
        ):
            associated_imported = True
        if not associado:
            associado, associated_imported = resolve_or_create_imported_associado(
                arquivo_nome=self.arquivo_retorno.arquivo_nome,
                competencia=competencia,
                data_geracao=self.arquivo_retorno.resultado_resumo.get("data_geracao"),
                cpf_cnpj=cpf,
                nome_completo=item.nome_servidor,
                matricula_orgao=item.matricula_servidor,
                orgao_publico=item.orgao_pagto_nome,
                cargo=item.cargo,
            )
        if not associado:
            item.associado = None
            item.parcela = None
            item.processado = True
            item.resultado_processamento = ArquivoRetornoItem.ResultadoProcessamento.NAO_ENCONTRADO
            item.observacao = "CPF não encontrado no cadastro de associados."
            item.save(
                update_fields=[
                    "associado",
                    "parcela",
                    "processado",
                    "resultado_processamento",
                    "observacao",
                    "updated_at",
                ]
            )
            ImportacaoLog.objects.create(
                arquivo_retorno=self.arquivo_retorno,
                tipo=ImportacaoLog.Tipo.RECONCILIACAO,
                mensagem="CPF não encontrado no cadastro de associados.",
                dados={
                    "linha_numero": item.linha_numero,
                    "cpf_cnpj": cpf,
                    "matricula": item.matricula_servidor,
                    "nome": item.nome_servidor,
                },
            )
            return {
                "resultado": ArquivoRetornoItem.ResultadoProcessamento.NAO_ENCONTRADO,
                "gerou_encerramento": False,
                "gerou_novo_ciclo": False,
                "associado_importado": associated_imported,
            }

        parcela = resolve_processing_competencia_parcela(
            associado_id=associado.id,
            referencia_mes=competencia,
            for_update=True,
        )
        if parcela is not None and is_synthetic_return_contract_code(
            getattr(parcela.ciclo.contrato, "codigo", "")
        ):
            parcela = None

        item.associado = associado
        item.parcela = parcela
        item.gerou_encerramento = False
        item.gerou_novo_ciclo = False

        if not parcela:
            outcome = self._processar_sem_parcela(item, associado)
        elif item.status_codigo == "1":
            outcome = self._processar_efetivado(item, parcela)
        elif item.status_codigo == "4":
            outcome = self._processar_efetivado(item, parcela, permitir_diferenca=True)
        elif item.status_codigo in {"2", "3", "S"}:
            outcome = self._processar_rejeitado(item, associado, parcela)
        elif item.status_codigo in {"5", "6"}:
            outcome = self._processar_pendencia_manual(item)
        else:
            outcome = self._processar_erro(item, f"Status ETIPI desconhecido: {item.status_codigo}")

        item.processado = True
        item.save()
        return {
            **outcome,
            "associado_importado": associated_imported,
        }

    def _processar_efetivado(
        self,
        item: ArquivoRetornoItem,
        parcela: Parcela,
        permitir_diferenca: bool = False,
    ) -> dict[str, object]:
        if parcela.status == Parcela.Status.LIQUIDADA:
            item.resultado_processamento = (
                ArquivoRetornoItem.ResultadoProcessamento.PENDENCIA_MANUAL
            )
            item.observacao = (
                "Competência vinculada a contrato liquidado. Revisão manual necessária."
            )
            return {
                "resultado": ArquivoRetornoItem.ResultadoProcessamento.PENDENCIA_MANUAL,
                "gerou_encerramento": False,
                "gerou_novo_ciclo": False,
                "associado_importado": False,
            }

        if item.valor_descontado != parcela.valor and not permitir_diferenca:
            if should_align_parcela_value_from_return(
                parcela=parcela,
                return_value=item.valor_descontado,
            ):
                valor_anterior = parcela.valor
                parcela.valor = item.valor_descontado
                parcela.observacao = self._append_note(
                    parcela.observacao,
                    (
                        "Valor da parcela alinhado automaticamente ao retorno "
                        f"{self.arquivo_retorno.arquivo_nome}: "
                        f"{valor_anterior} -> {item.valor_descontado}."
                    ),
                )
                parcela.save(update_fields=["valor", "observacao", "updated_at"])
            else:
                item.resultado_processamento = ArquivoRetornoItem.ResultadoProcessamento.PENDENCIA_MANUAL
                item.observacao = (
                    "Divergência de valor entre arquivo retorno e parcela. Revisão manual necessária."
                )
                ImportacaoLog.objects.create(
                    arquivo_retorno=self.arquivo_retorno,
                    tipo=ImportacaoLog.Tipo.RECONCILIACAO,
                    mensagem="Divergência de valor detectada.",
                    dados={
                        "linha_numero": item.linha_numero,
                        "valor_arquivo": str(item.valor_descontado),
                        "valor_parcela": str(parcela.valor),
                    },
                )
                return {
                    "resultado": ArquivoRetornoItem.ResultadoProcessamento.PENDENCIA_MANUAL,
                    "gerou_encerramento": False,
                    "gerou_novo_ciclo": False,
                    "associado_importado": False,
            }

        if parcela.status != Parcela.Status.DESCONTADO:
            parcela.status = Parcela.Status.DESCONTADO
            if not parcela.data_pagamento:
                parcela.data_pagamento = self.today
            nota = f"Baixa automática via arquivo retorno {self.arquivo_retorno.arquivo_nome}."
            if permitir_diferenca and item.valor_descontado != parcela.valor:
                nota = (
                    f"{nota} Valor do arquivo: {item.valor_descontado}. "
                    f"Valor da parcela: {parcela.valor}."
                )
            parcela.observacao = self._append_note(parcela.observacao, nota)
            parcela.save(update_fields=["status", "data_pagamento", "observacao", "updated_at"])
            propagate_competencia_status(parcela)

        self._regularizar_associado(
            parcela.associado,
            f"Situação regularizada via arquivo retorno {self.arquivo_retorno.arquivo_nome}.",
        )

        item.resultado_processamento = ArquivoRetornoItem.ResultadoProcessamento.BAIXA_EFETUADA
        if permitir_diferenca:
            item.observacao = "Parcela baixada automaticamente com divergência sinalizada pelo retorno."
        else:
            item.observacao = "Parcela baixada automaticamente."

        return {
            "resultado": ArquivoRetornoItem.ResultadoProcessamento.BAIXA_EFETUADA,
            "gerou_encerramento": False,
            "gerou_novo_ciclo": False,
            "associado_importado": False,
        }

    def _processar_rejeitado(
        self,
        item: ArquivoRetornoItem,
        associado: Associado,
        parcela: Parcela,
    ) -> dict[str, object]:
        if parcela.status != Parcela.Status.NAO_DESCONTADO:
            parcela.status = Parcela.Status.NAO_DESCONTADO
            parcela.data_pagamento = None
            parcela.observacao = self._append_note(parcela.observacao, item.status_descricao)
            parcela.save(update_fields=["status", "data_pagamento", "observacao", "updated_at"])
            propagate_competencia_status(parcela)

        associado.observacao = self._append_note(associado.observacao, item.status_descricao)
        associado.save(update_fields=["observacao", "updated_at"])
        self._associados_para_sync[associado.id] = associado

        item.motivo_rejeicao = item.status_descricao
        item.resultado_processamento = ArquivoRetornoItem.ResultadoProcessamento.NAO_DESCONTADO
        item.observacao = item.status_descricao
        return {
            "resultado": ArquivoRetornoItem.ResultadoProcessamento.NAO_DESCONTADO,
            "gerou_encerramento": False,
            "gerou_novo_ciclo": False,
            "associado_importado": False,
        }

    def _processar_sem_parcela(
        self,
        item: ArquivoRetornoItem,
        associado: Associado,
    ) -> dict[str, object]:
        nota_sem_parcela = (
            "Sem parcela local para a competência; o retorno foi aplicado sem alterar ciclos."
        )
        if item.status_codigo in {"1", "4"}:
            self._regularizar_associado(
                associado,
                f"Situação regularizada via arquivo retorno {self.arquivo_retorno.arquivo_nome}.",
            )
            item.resultado_processamento = ArquivoRetornoItem.ResultadoProcessamento.BAIXA_EFETUADA
            if item.status_codigo == "4":
                item.observacao = (
                    "Pagamento registrado via arquivo retorno com divergência sinalizada. "
                    f"{nota_sem_parcela}"
                )
            else:
                item.observacao = f"Pagamento registrado via arquivo retorno. {nota_sem_parcela}"
            ImportacaoLog.objects.create(
                arquivo_retorno=self.arquivo_retorno,
                tipo=ImportacaoLog.Tipo.RECONCILIACAO,
                mensagem="Item processado sem parcela local; ciclos preservados.",
                dados={"linha_numero": item.linha_numero, "cpf_cnpj": item.cpf_cnpj},
            )
            return {
                "resultado": ArquivoRetornoItem.ResultadoProcessamento.BAIXA_EFETUADA,
                "gerou_encerramento": False,
                "gerou_novo_ciclo": False,
                "associado_importado": False,
            }

        if item.status_codigo in {"2", "3", "S"}:
            associado.observacao = self._append_note(
                associado.observacao,
                item.status_descricao,
            )
            update_fields = ["observacao", "updated_at"]
            if associado.status == Associado.Status.IMPORTADO:
                associado.status = Associado.Status.INADIMPLENTE
                update_fields.append("status")
            else:
                self._associados_para_sync[associado.id] = associado
            associado.save(update_fields=update_fields)
            item.motivo_rejeicao = item.status_descricao
            item.resultado_processamento = ArquivoRetornoItem.ResultadoProcessamento.NAO_DESCONTADO
            item.observacao = f"{item.status_descricao}. {nota_sem_parcela}"
            ImportacaoLog.objects.create(
                arquivo_retorno=self.arquivo_retorno,
                tipo=ImportacaoLog.Tipo.RECONCILIACAO,
                mensagem="Item rejeitado sem parcela local; ciclos preservados.",
                dados={"linha_numero": item.linha_numero, "cpf_cnpj": item.cpf_cnpj},
            )
            return {
                "resultado": ArquivoRetornoItem.ResultadoProcessamento.NAO_DESCONTADO,
                "gerou_encerramento": False,
                "gerou_novo_ciclo": False,
                "associado_importado": False,
            }

        if item.status_codigo in {"5", "6"}:
            return self._processar_pendencia_manual(
                item,
                observacao_extra=nota_sem_parcela,
            )

        return self._processar_erro(item, f"Status ETIPI desconhecido: {item.status_codigo}")

    def _processar_pendencia_manual(
        self,
        item: ArquivoRetornoItem,
        *,
        observacao_extra: str = "",
    ) -> dict[str, object]:
        item.resultado_processamento = ArquivoRetornoItem.ResultadoProcessamento.PENDENCIA_MANUAL
        item.observacao = (
            f"{item.status_descricao}. {observacao_extra}".strip().strip(".")
            if observacao_extra
            else item.status_descricao
        )
        ImportacaoLog.objects.create(
            arquivo_retorno=self.arquivo_retorno,
            tipo=ImportacaoLog.Tipo.RECONCILIACAO,
            mensagem="Pendência manual criada pelo arquivo retorno.",
            dados={"linha_numero": item.linha_numero, "status_codigo": item.status_codigo},
        )
        return {
            "resultado": ArquivoRetornoItem.ResultadoProcessamento.PENDENCIA_MANUAL,
            "gerou_encerramento": False,
            "gerou_novo_ciclo": False,
            "associado_importado": False,
        }

    def _processar_erro(self, item: ArquivoRetornoItem, mensagem: str) -> dict[str, object]:
        item.resultado_processamento = ArquivoRetornoItem.ResultadoProcessamento.ERRO
        item.observacao = mensagem
        ImportacaoLog.objects.create(
            arquivo_retorno=self.arquivo_retorno,
            tipo=ImportacaoLog.Tipo.ERRO,
            mensagem=mensagem,
            dados={"linha_numero": item.linha_numero},
        )
        return {
            "resultado": ArquivoRetornoItem.ResultadoProcessamento.ERRO,
            "gerou_encerramento": False,
            "gerou_novo_ciclo": False,
            "associado_importado": False,
        }

    def _regularizar_associado(self, associado: Associado | None, note: str) -> None:
        if associado is None:
            return
        if associado.status == Associado.Status.INATIVO:
            return
        associado.observacao = self._append_note(associado.observacao, note)
        associado.save(update_fields=["observacao", "updated_at"])
        self._associados_para_sync[associado.id] = associado

    @staticmethod
    def _append_note(base: str, note: str) -> str:
        if not base:
            return note
        if note in base:
            return base
        return f"{base}\n{note}"
