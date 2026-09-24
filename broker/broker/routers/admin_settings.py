"""Owner routes for operator settings (the console's Settings view).

`GET` lists every console-editable setting with its value, env default and
bounds, plus the env-only keys and why each lives in a file. `PATCH`
validates every field before writing any, and is audited with the changed
names. Guarded router-wide by `require_admin`.
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from .. import runtime_settings
from ..deps import AdminContext, require_admin

router = APIRouter(dependencies=[Depends(require_admin)])


class SettingsPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # {name: value}; null resets a setting to its env default.
    settings: dict


@router.get("/v1/admin/settings")
def get_settings_view() -> dict:
    return runtime_settings.describe()


@router.patch("/v1/admin/settings")
def patch_settings(body: SettingsPatch, ctx: AdminContext = Depends(require_admin)) -> dict:
    return runtime_settings.update(ctx, body.settings)
