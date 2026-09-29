"""多 Agent 协作：角色定义与主管编排。"""

from server_agent.multi.roles import DIAGNOSTICIAN, EXECUTOR, REVIEWER, ROLES, Role, registry_for
from server_agent.multi.supervisor import MultiRunResult, RoleRun, Supervisor

__all__ = ["DIAGNOSTICIAN", "EXECUTOR", "REVIEWER", "ROLES", "MultiRunResult", "Role", "RoleRun",
           "Supervisor", "registry_for"]
