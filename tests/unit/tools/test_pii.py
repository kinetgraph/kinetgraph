# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Tests for `tools/pii.py` — `PiiRedactionTool` (Fase 3).

The tests cover:

  - Level 1 (regex) redaction of CPF, CNPJ, e-mail,
    phone, CEP, PIX key, credit card, and arbitrary
    non-PII strings.
  - The recursive walker (dicts, lists, nested
    structures).
  - Order of patterns: more-specific first (CNPJ before
    CEP, PIX before CEP) so one pattern does not eat
    the input of another.
  - `Tool` Protocol compliance (the tool is registered
    in the framework's `Tool` surface).
  - `invoke` returns `Ok(RedactionResult)` on success
    and `Err(ToolError)` on failure.
  - Validation: level ∈ {1, 2, 3}, labels non-empty.
  - Level 2/3 require an `EntityExtractor` (auto-
    instantiated as Heuristic if not provided).
  - `redact` returns a `RedactionResult` with the
    redacted payload, the `counts` map, and the
    `level` echo.
  - Idempotency: re-running on already-redacted text
    is a no-op (placeholders do not match the regex).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kntgraph.agents.tools.pii import (
    DEFAULT_PII_LABELS,
    PiiRedactionTool,
    RedactionResult,
)
from kntgraph.agents.tools.protocol import Tool


# ---------------------------------------------------------------------------
# Level 1 — regex
# ---------------------------------------------------------------------------


class TestLevel1Regex:
    def setup_method(self) -> None:
        self.tool = PiiRedactionTool(level=1)

    @pytest.mark.asyncio
    async def test_redacts_cnpj(self) -> None:
        r = await self.tool.redact({"cnpj": "12.345.678/0001-90"})
        assert r.redacted == {"cnpj": "<PII:cnpj>"}
        assert r.counts == {"cnpj": 1}
        assert r.level == 1

    @pytest.mark.asyncio
    async def test_redacts_cpf(self) -> None:
        r = await self.tool.redact({"cpf": "123.456.789-09"})
        assert r.redacted == {"cpf": "<PII:cpf>"}
        assert r.counts == {"cpf": 1}

    @pytest.mark.asyncio
    async def test_redacts_email(self) -> None:
        r = await self.tool.redact({"email": "joao@example.com"})
        assert r.redacted == {"email": "<PII:email>"}
        assert r.counts == {"email": 1}

    @pytest.mark.asyncio
    async def test_redacts_phone_br_with_country_code(self) -> None:
        r = await self.tool.redact({"phone": "+55 11 91234-5678"})
        assert r.redacted == {"phone": "<PII:telefone>"}
        assert r.counts == {"telefone": 1}

    @pytest.mark.asyncio
    async def test_redacts_cep_with_dash(self) -> None:
        r = await self.tool.redact({"cep": "01310-100"})
        assert r.redacted == {"cep": "<PII:cep>"}

    @pytest.mark.asyncio
    async def test_redacts_cep_without_dash(self) -> None:
        r = await self.tool.redact({"cep": "01310100"})
        assert r.redacted == {"cep": "<PII:cep>"}

    @pytest.mark.asyncio
    async def test_redacts_pix_key(self) -> None:
        r = await self.tool.redact(
            {
                "pix": ("12345678-1234-1234-1234-123456789012"),
            }
        )
        assert r.redacted == {
            "pix": "<PII:chave_pix>",
        }
        assert r.counts == {"chave_pix": 1}

    @pytest.mark.asyncio
    async def test_redacts_credit_card_space_separated(self) -> None:
        r = await self.tool.redact({"card": "4111 1111 1111 1111"})
        assert r.redacted == {"card": "<PII:cartao_credito>"}

    @pytest.mark.asyncio
    async def test_redacts_credit_card_dash_separated(self) -> None:
        r = await self.tool.redact({"card": "4111-1111-1111-1111"})
        assert r.redacted == {"card": "<PII:cartao_credito>"}

    @pytest.mark.asyncio
    async def test_does_not_redact_arbitrary_text(self) -> None:
        r = await self.tool.redact({"nota": "NF-00123", "valor": 100.50})
        assert r.redacted == {"nota": "NF-00123", "valor": 100.50}
        assert r.counts == {}

    @pytest.mark.asyncio
    async def test_pix_not_eaten_by_cep(self) -> None:
        # The CEP regex must not eat the first 8 digits
        # of a PIX key (a known false-positive case).
        r = await self.tool.redact("chave: 12345678-1234-1234-1234-123456789012")
        # Exactly one redaction, the PIX key, not the
        # 8-digit prefix.
        assert r.counts == {"chave_pix": 1}
        assert "<PII:chave_pix>" in r.redacted
        assert "<PII:cep>" not in r.redacted

    @pytest.mark.asyncio
    async def test_recursive_walker(self) -> None:
        payload = {
            "fornecedor": {
                "cnpj": "12.345.678/0001-90",
                "email": "x@y.com",
            },
            "itens": [
                {"valor": 1.0},
                {"valor": 2.0, "obs": "NF-001 cnpj 11.111.111/0001-11"},
            ],
        }
        r = await self.tool.redact(payload)
        assert r.redacted["fornecedor"]["cnpj"] == "<PII:cnpj>"
        assert r.redacted["fornecedor"]["email"] == "<PII:email>"
        assert r.redacted["itens"][0]["valor"] == 1.0
        # The nested `obs` string has BOTH NF-001 (not
        # PII) and a CNPJ (PII). Only the CNPJ should
        # be redacted.
        assert "NF-001" in r.redacted["itens"][1]["obs"]
        assert "<PII:cnpj>" in r.redacted["itens"][1]["obs"]

    @pytest.mark.asyncio
    async def test_idempotent_on_already_redacted(self) -> None:
        # The placeholders do not match the regexes,
        # so re-running is a no-op.
        once = await self.tool.redact({"x": "12.345.678/0001-90"})
        twice = await self.tool.redact(once.redacted)
        assert twice.redacted == once.redacted
        assert twice.counts == {}

    @pytest.mark.asyncio
    async def test_preserves_non_pii_scalars(self) -> None:
        r = await self.tool.redact(
            {"int": 42, "float": 3.14, "bool": True, "none": None}
        )
        assert r.redacted == {
            "int": 42,
            "float": 3.14,
            "bool": True,
            "none": None,
        }
        assert r.counts == {}


# ---------------------------------------------------------------------------
# Level 2 / 3 — extractor path
# ---------------------------------------------------------------------------


class TestLevel2Extractor:
    def test_default_extractor_is_heuristic(self) -> None:
        tool = PiiRedactionTool(level=2)
        # Lazy default: instantiated on first `redact`.
        # We just confirm the constructor accepted
        # the level.
        assert tool._level == 2

    @pytest.mark.asyncio
    async def test_extractor_used_for_nome_pessoa_label(self) -> None:
        # The default PII label set does NOT include
        # "org" — that's an NER label, not a PII label
        # by itself. PII is what the company sees, not
        # the company name. The "nome_pessoa" label
        # IS in the PII set; we use that for the
        # redaction test.
        class FakeExtractor:
            async def extract(self, text):
                return [
                    SimpleNamespace(
                        type="nome_pessoa",
                        name="joão da silva",
                        surface="João da Silva",
                    )
                ]

        tool = PiiRedactionTool(level=2, entity_extractor=FakeExtractor())
        r = await tool.redact({"contato": "Falar com João da Silva"})
        assert r.redacted == {
            "contato": "Falar com <PII:nome_pessoa>",
        }
        assert r.counts.get("nome_pessoa") == 1

    @pytest.mark.asyncio
    async def test_extractor_with_custom_labels(self) -> None:
        # The constructor accepts a custom label set.
        # An extractor returning a type in the custom
        # set IS used.
        class FakeExtractor:
            async def extract(self, text):
                return [
                    SimpleNamespace(
                        type="org",
                        name="acme s/a",
                        surface="ACME S/A",
                    )
                ]

        tool = PiiRedactionTool(
            level=2,
            entity_extractor=FakeExtractor(),
            labels=("org", "person"),  # custom set
        )
        r = await tool.redact({"x": "ACME S/A"})
        assert r.redacted == {"x": "<PII:org>"}
        assert r.counts.get("org") == 1

    @pytest.mark.asyncio
    async def test_extractor_filters_unexpected_types(self) -> None:
        # An extractor that returns an entity with a
        # type not in the label set. The redaction
        # should ignore it.
        class FakeExtractor:
            async def extract(self, text):
                return [
                    SimpleNamespace(
                        type="weird_type",
                        name="acme s/a",
                        surface="ACME S/A",
                    )
                ]

        tool = PiiRedactionTool(
            level=2,
            entity_extractor=FakeExtractor(),
            labels=DEFAULT_PII_LABELS,  # "weird_type" not in
        )
        r = await tool.redact({"x": "ACME S/A"})
        assert r.redacted == {"x": "ACME S/A"}
        assert r.counts == {}


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestValidation:
    def test_level_must_be_1_2_or_3(self) -> None:
        with pytest.raises(ValueError, match="level must be"):
            PiiRedactionTool(level=4)
        with pytest.raises(ValueError, match="level must be"):
            PiiRedactionTool(level=0)

    def test_labels_must_be_non_empty(self) -> None:
        with pytest.raises(ValueError, match="labels must be"):
            PiiRedactionTool(labels=())


# ---------------------------------------------------------------------------
# Tool Protocol
# ---------------------------------------------------------------------------


class TestToolProtocol:
    def test_satisfies_protocol(self) -> None:
        tool = PiiRedactionTool(level=1)
        assert isinstance(tool, Tool)
        # The Protocol's required attributes are
        # present.
        assert tool.name == "fmh.pii.redact"
        assert "payload" in tool.input_schema["properties"]
        assert tool.input_schema["required"] == ["payload"]

    @pytest.mark.asyncio
    async def test_invoke_ok(self) -> None:
        tool = PiiRedactionTool(level=1)
        r = await tool.invoke(
            idempotency_key="k",
            payload={"cnpj": "12.345.678/0001-90"},
        )
        assert r.is_ok()
        rr = r.unwrap()
        assert isinstance(rr, RedactionResult)
        assert rr.redacted == {"cnpj": "<PII:cnpj>"}

    @pytest.mark.asyncio
    async def test_invoke_fail_closed_on_redactor_raise(self) -> None:
        class BoomExtractor:
            async def extract(self, text):
                raise RuntimeError("extractor down")

        tool = PiiRedactionTool(level=2, entity_extractor=BoomExtractor())
        # We need to trigger the extractor; an empty
        # payload short-circuits. Use a string that
        # the heuristic would not match (so the
        # extractor would be called).
        r = await tool.invoke(
            idempotency_key="k",
            payload={"x": "some text with no PII"},
        )
        # The level-1 regex redacts nothing; the
        # level-2 extractor raises; the wrapper turns
        # that into `Err`.
        assert r.is_err()


# ---------------------------------------------------------------------------
# Env-driven level
# ---------------------------------------------------------------------------


class TestEnvLevel:
    def test_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KNT_PII_LEVEL", "3")
        tool = PiiRedactionTool()
        assert tool._level == 3
