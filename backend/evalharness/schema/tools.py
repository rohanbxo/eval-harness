"""Tool definition and mock-configuration models (SPEC 4.2, 5.2, 5.3)."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from evalharness.schema.matchers import ArgMatchers


class FixtureMock(BaseModel):
    """Table-driven mock: first matching response wins (SPEC 5.2)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["fixture"] = "fixture"
    file: str = Field(description="Fixture path relative to the scenario directory.")


class HandlerMock(BaseModel):
    """Python-callable mock for tools that need real logic or state (SPEC 5.3)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["handler"] = "handler"
    name: str = Field(description="Registered handler name, e.g. 'sqlite_query'.")
    config: dict[str, JsonValue] = Field(default_factory=dict)


MockConfig = Annotated[FixtureMock | HandlerMock, Field(discriminator="kind")]


class ToolDefinition(BaseModel):
    """One tool: OpenAI function-calling shape plus how it is mocked."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    description: str
    parameters: dict[str, Any] = Field(description="JSON Schema for the arguments object.")
    mock: MockConfig

    def to_openai_schema(self) -> dict[str, Any]:
        """The provider-facing shape. The ``mock`` key never reaches the model."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class FixtureResponse(BaseModel):
    """One row of a fixture table."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    match: ArgMatchers = Field(default_factory=dict)
    response: JsonValue
    ok: bool = Field(default=True, description="False marks this response as a tool error.")


class FixtureFile(BaseModel):
    """Contents of a fixture file referenced by ``FixtureMock.file``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    responses: list[FixtureResponse] = Field(default_factory=list)
    default: JsonValue = None
    default_ok: bool = Field(
        default=False,
        description="Whether the fallback response counts as success. Defaults to error.",
    )
