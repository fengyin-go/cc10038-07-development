"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。"""
from __future__ import annotations

from app.routers import resource, pipeline_router, lineage_router

# 血缘/重放含 /api/lineage 固定前缀，先于动态 /api/{module} 注册，
# 保证具体路径不会被通用资源路由抢先匹配。
ROUTERS = [lineage_router, pipeline_router, resource]
