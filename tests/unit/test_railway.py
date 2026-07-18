# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Testes unitários para Railway Pattern e Result.
"""

import uuid

import pytest
from typing import cast

from kntgraph.core.event.correlation import CorrelationContext
from kntgraph.core.result import (
    Result,
    Ok,
    Err,
    ValidationError,
    BusinessError,
    PersistenceError,
)


class TestResultBasic:
    """Testes básicos de Result."""

    def test_ok_creation(self):
        """Deve criar Result de sucesso."""
        result = Ok(42)
        assert result.is_ok()
        assert not result.is_err()
        assert result.ok_value() == 42

    def test_err_creation(self):
        """Deve criar Result de erro."""
        result = Err(ValueError("test error"))
        assert result.is_err()
        assert not result.is_ok()
        assert isinstance(result.err_value(), ValueError)

    def test_ok_with_custom_error(self):
        """Deve criar Ok com erro customizado."""
        result = Ok("value")
        assert result.ok_value() == "value"

    def test_err_with_custom_error(self):
        """Deve criar Err com erro customizado."""
        error = BusinessError("business rule violated")
        result = Err(error)
        assert isinstance(result.err_value(), BusinessError)
        assert str(result.err_value()) == "business rule violated"


class TestResultTry:
    """Testes de Result.try_."""

    def test_try_success(self):
        """Deve capturar sucesso."""
        result = Result.try_(lambda: 10 / 2)
        assert result.is_ok()
        assert result.ok_value() == 5.0

    def test_try_exception(self):
        """Deve capturar exceção."""
        result = Result.try_(lambda: 10 / 0)
        assert result.is_err()
        assert isinstance(result.err_value(), ZeroDivisionError)

    def test_try_with_custom_exception(self):
        """Deve capturar exceção customizada."""

        def raise_custom():
            raise ValidationError("invalid data")

        result = Result.try_(raise_custom)
        assert result.is_err()
        assert isinstance(result.err_value(), ValidationError)

    def test_try_with_specific_exception_type(self):
        """Deve capturar apenas exceções específicas."""
        result = Result.try_(lambda: 10 / 0, exception_type=ZeroDivisionError)
        assert result.is_err()
        assert isinstance(result.err_value(), ZeroDivisionError)


class TestResultMap:
    """Testes de map (transformação de sucesso)."""

    def test_map_success(self):
        """Deve transformar valor de sucesso."""
        result = Ok(5).map(lambda x: x * 2)
        assert result.is_ok()
        assert result.ok_value() == 10

    def test_map_on_error(self):
        """Não deve transformar erro."""
        result = Err(ValueError("error")).map(lambda x: x * 2)
        assert result.is_err()
        assert isinstance(result.err_value(), ValueError)

    def test_map_chain(self):
        """Deve encadear transformações."""
        result = Ok(1).map(lambda x: x + 1).map(lambda x: x * 2).map(lambda x: x + 10)
        assert result.ok_value() == 14


class TestResultMapErr:
    """Testes de map_err (transformação de erro)."""

    def test_map_err_on_success(self):
        """Não deve transformar sucesso."""
        result = Ok(42).map_err(lambda e: RuntimeError(str(e)))
        assert result.is_ok()
        assert result.ok_value() == 42

    def test_map_err_on_error(self):
        """Deve transformar erro."""
        result = Err(ValueError("original")).map_err(
            lambda e: RuntimeError(f"wrapped: {e}")
        )
        assert result.is_err()
        assert isinstance(result.err_value(), RuntimeError)
        assert str(result.err_value()) == "wrapped: original"


class TestResultBind:
    """Testes de bind (flatMap/chain)."""

    def test_bind_success(self):
        """Deve encadear operações de sucesso."""
        result = Ok(5).bind(lambda x: Ok(x + 10))
        assert result.is_ok()
        assert result.ok_value() == 15

    def test_bind_on_error(self):
        """Não deve executar bind em erro."""
        result = Err(ValueError("error")).bind(lambda x: Ok(x + 10))
        assert result.is_err()
        assert isinstance(result.err_value(), ValueError)

    def test_bind_multiple(self):
        """Deve encadear múltiplas operações."""
        result = (
            Ok(1)
            .bind(lambda x: Ok(x + 1))
            .bind(lambda x: Ok(x * 2))
            .bind(lambda x: Ok(x + 10))
        )
        assert result.is_ok()
        assert result.ok_value() == 14

    def test_bind_with_error_in_chain(self):
        """Deve parar no primeiro erro."""
        result = (
            Ok(1)
            .bind(lambda x: Ok(x + 1))
            .bind(lambda x: Err(ValueError("middle error")))
            .bind(lambda x: Ok(x * 2))  # Não executa
        )
        assert result.is_err()
        assert str(result.err_value()) == "middle error"


class TestResultValueOr:
    """Testes de value_or (fallback)."""

    def test_value_or_with_success(self):
        """Deve retornar valor se sucesso."""
        result: int | str = Ok(42).value_or(cast(int, "default"))
        assert result == 42

    def test_value_or_with_error(self):
        """Deve retornar default se erro."""
        result: int | str = Err(ValueError("error")).value_or(cast(int, "default"))
        assert result == "default"


class TestResultUnwrap:
    """Testes de unwrap."""

    def test_unwrap_success(self):
        """Deve retornar valor se sucesso."""
        result = Ok(42)
        assert result.unwrap() == 42

    def test_unwrap_error(self):
        """Deve levantar exceção se erro."""
        result = Err(ValueError("error"))
        with pytest.raises(Exception):  # UnwrapError
            result.unwrap()

    def test_err_value_or_raise_returns_error(self):
        """On Err, returns the error directly (no None)."""
        err = ValueError("boom")
        result = Err(err)
        assert result.is_err()
        assert result.err_value_or_raise() is err

    def test_err_value_or_raise_raises_on_ok(self):
        """On Ok, raises UnwrapError."""
        result: Result[int, ValueError] = Ok(42)
        assert result.is_ok()
        with pytest.raises(Exception):  # UnwrapError
            result.err_value_or_raise()

    def test_err_value_or_raise_with_tool_error(self):
        """Common case: ToolError is the error type."""
        from kntgraph.core.result import ToolError

        err = ToolError("rate_limited")
        result = Err(err)
        assert result.err_value_or_raise() is err

    def test_unwrap_or_success(self):
        """Deve retornar valor se sucesso."""
        result: int | str = Ok(42).unwrap_or(cast(int, "default"))
        assert result == 42

    def test_unwrap_or_error(self):
        """Deve retornar default se erro."""
        result = Err(ValueError("error")).unwrap_or("default")
        assert result == "default"

    def test_unwrap_or_else_success(self):
        """Deve retornar valor se sucesso."""
        result: int | str = Ok(42).unwrap_or_else(lambda e: cast(int, "default"))
        assert result == 42

    def test_unwrap_or_else_error(self):
        """Deve executar função se erro."""
        result = Err(ValueError("error")).unwrap_or_else(lambda e: f"got: {e}")
        assert result == "got: error"


class TestResultExpect:
    """Testes de expect."""

    def test_expect_success(self):
        """Deve retornar valor se sucesso."""
        result = Ok(42)
        assert result.expect("should succeed") == 42

    def test_expect_error(self):
        """Deve levantar exceção com mensagem customizada."""
        result = Err(ValueError("error"))
        with pytest.raises(Exception, match="custom message"):
            result.expect("custom message")

    def test_expect_error_with_none_value(self):
        """An Err result should still raise when the wrapped value is missing."""
        result = Err(ValueError("error"))
        with pytest.raises(Exception, match="missing"):
            result.expect("missing")


class TestResultMatch:
    """Testes de pattern matching."""

    def test_match_success(self):
        """Deve executar ok_func se sucesso."""
        executed = []

        Ok(42).match(
            lambda v: executed.append(("ok", v)), lambda e: executed.append(("err", e))
        )

        assert executed == [("ok", 42)]

    def test_match_error(self):
        """Deve executar err_func se erro."""
        executed = []

        Err(ValueError("error")).match(
            lambda v: executed.append(("ok", v)),
            lambda e: executed.append(("err", type(e).__name__)),
        )

        assert executed == [("err", "ValueError")]

    def test_match_returns_value(self):
        """Deve retornar valor da função executada."""
        result = Ok(42).match(lambda v: v * 2, lambda e: 0)
        assert result == 84

    def test_match_returns_none_for_indeterminate_result(self):
        """A match should return None when neither branch is available."""

        class NeutralResult:
            def is_ok(self) -> bool:
                return False

            def is_err(self) -> bool:
                return False

            def ok(self) -> None:
                return None

            def err(self) -> None:
                return None

        result = Result[object, ValueError](NeutralResult())
        assert result.match(lambda v: "ok", lambda e: "err") is None


class TestRailwayComposition:
    """Testes de composição Railway."""

    def test_composition_all_success(self):
        """Deve compor múltiplas operações de sucesso."""

        def step1() -> Result[int, ValidationError]:
            return Ok(1)

        def step2(x: int) -> Result[int, ValidationError]:
            return Ok(x + 1)

        def step3(x: int) -> Result[int, ValidationError]:
            return Ok(x * 2)

        result = step1().bind(step2).bind(step3)

        assert result.is_ok()
        assert result.ok_value() == 4

    def test_composition_with_failure(self):
        """Deve propagar erro na composição."""

        def step1() -> Result[int, ValidationError]:
            return Ok(1)

        def step2(x: int) -> Result[int, ValidationError]:
            return Err(ValidationError("step2 failed"))

        def step3(x: int) -> Result[int, ValidationError]:
            return Ok(x * 2)  # Não executa

        result = step1().bind(step2).bind(step3)

        assert result.is_err()
        assert str(result.err_value()) == "step2 failed"

    def test_composition_with_transformation(self):
        """Deve compor com transformações."""
        result = (
            Ok(10).map(lambda x: x - 5).bind(lambda x: Ok(x / 2)).map(lambda x: x + 100)
        )

        assert result.is_ok()
        assert result.ok_value() == 102.5


class TestCustomErrorTypes:
    """Testes com tipos de erro customizados."""

    def test_validation_error(self):
        """Deve usar ValidationError."""
        result = Err(ValidationError("field required"))
        assert isinstance(result.err_value(), ValidationError)

    def test_business_error(self):
        """Deve usar BusinessError."""
        result = Err(BusinessError("rule violated"))
        assert isinstance(result.err_value(), BusinessError)

    def test_persistence_error(self):
        """Deve usar PersistenceError."""
        result = Err(PersistenceError("database connection failed"))
        assert isinstance(result.err_value(), PersistenceError)

    def test_error_type_preservation(self):
        """Deve preservar tipo de erro através de operações."""
        result = (
            Err(ValidationError("test"))
            .map(lambda x: x + 1)  # Não executa
            .bind(lambda x: Ok(x))  # Não executa
        )

        assert result.is_err()
        assert isinstance(result.err_value(), ValidationError)


class TestRailwayPatternExamples:
    """Exemplos práticos de Railway Pattern."""

    def test_example_data_pipeline(self):
        """Exemplo: Pipeline de processamento de dados."""

        def validate(data: dict) -> Result[dict, ValidationError]:
            if not data.get("id"):
                return Err(ValidationError("id required"))
            return Ok(data.copy())

        def transform(data: dict) -> Result[dict, ValidationError]:
            new_data = data.copy()
            new_data["transformed"] = True
            return Ok(new_data)

        def save(data: dict) -> Result[str, ValidationError]:
            return Ok(f"saved-{data['id']}")

        # Pipeline
        result = validate({"id": "123"}).bind(transform).bind(save)

        assert result.is_ok()
        assert result.ok_value() == "saved-123"

    def test_example_error_handling(self):
        """Exemplo: Tratamento de erro com fallback."""

        def risky_operation() -> Result[int, ValidationError]:
            return Err(ValidationError("operation failed"))

        result = risky_operation().map_err(lambda e: f"logged: {e}").value_or(42)

        assert result == 42

    def test_example_multiple_validations(self):
        """Exemplo: Múltiplas validações."""

        def validate_email(email: str) -> Result[str, ValidationError]:
            if "@" not in email:
                return Err(ValidationError("invalid email"))
            return Ok(email)

        def validate_age(age: int) -> Result[int, ValidationError]:
            if age < 18:
                return Err(ValidationError("must be 18+"))
            return Ok(age)

        def validate_name(name: str) -> Result[str, ValidationError]:
            if not name:
                return Err(ValidationError("name required"))
            return Ok(name)

        # Todas validações devem passar
        email_result = validate_email("test@example.com")
        age_result = validate_age(25)
        name_result = validate_name("John")

        assert email_result.is_ok()
        assert age_result.is_ok()
        assert name_result.is_ok()

        # Uma validação falha
        failed_result = validate_age(15)
        assert failed_result.is_err()


class TestAsyncRailway:
    """Testes para Railway Pattern async (conceitual)."""

    @pytest.mark.asyncio
    async def test_async_result_chain(self):
        """Deve encadear operações async."""

        async def fetch_data() -> Result[dict, ValidationError]:
            return Ok({"id": "123"})

        async def process_data(data: dict) -> Result[dict, ValidationError]:
            data["processed"] = True
            return Ok(data)

        # Nota: bind não funciona diretamente com async
        # Padrão recomendado: if/else
        result1 = await fetch_data()
        if result1.is_err():
            err1 = result1.err_value()
            if err1 is not None:
                return Err(err1)

        data1 = result1.ok_value()
        assert data1 is not None
        result2 = await process_data(data1)
        if result2.is_err():
            err2 = result2.err_value()
            if err2 is not None:
                return Err(err2)

        data2 = result2.ok_value()
        assert data2 is not None
        assert data2["processed"] is True


# ============================================================================
# Integration Tests
# ============================================================================


class TestRailwayIntegration:
    """
    Integration of the Railway pattern with FMH core.

    In v2.0 the World has no outbox; events are pure values and the
    runner appends them to the stream. Railway patterns are useful
    at the I/O boundary (e.g. wrapping adapter calls) — not at the
    world fold, which must stay pure.
    """

    def test_world_fold_pure(self):
        """World.fold is a pure function: same events → same world."""
        from kntgraph.core.event import Event
        from kntgraph.core.world import World

        events = [
            Event.create(
                correlation=CorrelationContext.new(correlation_id=uuid.uuid4()),
                event_type="agent.spawned",
                agent_id="a-1",
                event_class="lifecycle",
                data={},
            ),
            Event.create(
                correlation=CorrelationContext.new(correlation_id=uuid.uuid4()),
                event_type="document.received",
                agent_id="a-1",
                event_class="domain",
                data={"document_id": "NF-001"},
            ),
        ]
        w1 = World.fold(events)
        w2 = World.fold(events)
        assert w1.tick == w2.tick
        assert list(w1.agents.keys()) == list(w2.agents.keys())
        assert w1.agents["a-1"].operational_phase == "spawned"
        assert w1.agents["a-1"].domain_phase == "document.received"

    def test_result_wraps_constructor_failure(self):
        """Result.try_ captures constructor errors as Err."""
        from kntgraph.core.result import Result

        def bad_ctor() -> int:
            raise ValueError("bad input")

        r = Result.try_(bad_ctor)
        assert r.is_err()
        assert "bad input" in str(r.err_value())

    def test_result_bind_chains_with_ok(self):
        """Bind chains Ok values; Err short-circuits."""
        from kntgraph.core.result import Ok

        r = Ok(2).bind(lambda x: Ok(x * 10)).bind(lambda x: Ok(x + 1))
        assert r.is_ok()
        assert r.unwrap() == 21

    def test_result_bind_short_circuits_err(self):
        from kntgraph.core.result import Err, Ok, RailwayError

        class MyErr(RailwayError):
            pass

        r = Ok(2).bind(lambda x: Err(MyErr("stop"))).bind(lambda x: Ok(x + 1))
        assert r.is_err()
        assert "stop" in str(r.err_value())


# ============================================================================
# Run Tests
# ============================================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
