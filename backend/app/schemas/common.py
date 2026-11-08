from typing import Annotated, ClassVar
from pydantic import BaseModel, ConfigDict, Field, model_validator


RecordId = Annotated[int, Field(strict=True, gt=0, le=9999999999999999999)]


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class PatchModel(InputModel):
    nullable_fields: ClassVar[set[str]] = {"email"}
    @model_validator(mode="after")
    def validate_changes(self):
        if not self.model_fields_set:
            raise ValueError("En az bir değişiklik gönderilmelidir.")
        for name in self.model_fields_set:
            if name not in self.nullable_fields and getattr(self, name) is None:
                raise ValueError("Zorunlu alan null olamaz.")
        return self
