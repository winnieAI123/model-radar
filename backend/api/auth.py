"""HTTP Basic Auth 依赖。DASHBOARD_PASS 为空时直接放行（本地调试）。"""
import secrets

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from backend.utils import config

_security = HTTPBasic(auto_error=False)


def require_auth(creds: HTTPBasicCredentials | None = Depends(_security)) -> str:
    if not config.DASHBOARD_PASS:
        return "anonymous"  # auth 禁用，放行
    if creds is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Auth required",
            headers={"WWW-Authenticate": "Basic"},
        )
    ok_user = secrets.compare_digest(creds.username, config.DASHBOARD_USER)
    ok_pass = secrets.compare_digest(creds.password, config.DASHBOARD_PASS)
    if not (ok_user and ok_pass):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return creds.username
