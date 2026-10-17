"""Who is calling, and may they? Every endpoint except login declares the role it needs:

- ``Viewer``: read anything in their own tenant
- ``Reviewer``: + review findings, rules and relationships, add rules, answer questions
- ``Admin``: + manage people, read the audit log

A request without a valid token gets 401; a valid one with too small a role gets 403.
"""

from collections.abc import Callable, Iterator
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from lumida.accounts.store import Principal, resolve
from lumida.graph.models import Role


def get_session(request: Request) -> Iterator[Session]:
    with Session(request.app.state.engine) as session:  # closing rolls back uncommitted work
        yield session


DB = Annotated[Session, Depends(get_session)]
Credentials = Annotated[HTTPAuthorizationCredentials | None, Depends(HTTPBearer(auto_error=False))]
UNAUTHORIZED = HTTPException(401, "log in first (missing, expired or unknown token)",
                             headers={"WWW-Authenticate": "Bearer"})  # fmt: skip


def current_principal(credentials: Credentials, session: DB) -> Principal:
    if credentials is None:
        raise UNAUTHORIZED
    principal = resolve(session, credentials.credentials)
    if principal is None:
        raise UNAUTHORIZED
    return principal


def need(role: Role) -> Callable[[Principal], Principal]:
    def check(principal: Annotated[Principal, Depends(current_principal)]) -> Principal:
        if not principal.can(role):
            raise HTTPException(403, f"this needs the {role.value} role "
                                     f"(you are {principal.role.value})")  # fmt: skip
        return principal

    return check


Viewer = Annotated[Principal, Depends(need(Role.VIEWER))]
Reviewer = Annotated[Principal, Depends(need(Role.REVIEWER))]
Admin = Annotated[Principal, Depends(need(Role.ADMIN))]
