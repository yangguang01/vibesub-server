from datetime import datetime, timezone
from fastapi import HTTPException
import pytz
from typing import Dict, Any, List, Optional, Union
from app.common.utils.firestore_init import db
from app.common.core.logging import logger
from google.cloud.firestore_v1.base_query import FieldFilter
from google.cloud import firestore
from google.cloud.firestore_v1 import Increment, ArrayUnion, SERVER_TIMESTAMP
from google.api_core.exceptions import NotFound
from app.common.core.config import DEFAULT_DAILY_LIMIT


# 集合名称常量
TASKS_COLLECTION = "tasks"
USER_TASKS_COLLECTION = "user_tasks"
ANALYTICS_COLLECTION = "analytics"

# 集合名称常量
USERINFO_COLLECTION = "userinfo"


def _format_beijing(utc_dt: datetime) -> str:
    beijing = pytz.timezone("Asia/Shanghai")
    return utc_dt.replace(tzinfo=pytz.utc).astimezone(beijing).strftime("%Y-%m-%d %H:%M:%S")


def get_date_string(dt: Optional[datetime] = None) -> str:
    """
    获取日期字符串(中国时区)
    
    参数:
        dt: 时间对象，如果为None则使用当前时间
        
    返回:
        str: 格式化的日期字符串 (YYYY-MM-DD_CST)
    """
    if dt is None:
        dt = datetime.now(timezone.utc)
    elif dt.tzinfo is None:
        # 如果没有时区信息，假定是UTC
        dt = dt.replace(tzinfo=timezone.utc)
        
    beijing_tz = pytz.timezone('Asia/Shanghai')
    beijing_dt = dt.astimezone(beijing_tz)
    return beijing_dt.strftime("%Y-%m-%d_CST")


def save_task(task_id: str, task_data: Dict[str, Any], video_id: Optional[str] = None) -> bool:
    """
    保存任务数据到Firestore
    
    参数:
        task_id: 任务ID
        task_data: 任务数据
        video_id: 视频ID(可选)，如果不提供则使用task_id
        
    返回:
        bool: 是否保存成功
    """
    try:
        # 获取视频ID
        if video_id is None:
            video_id = task_data.get("video_id", task_id)
        
        # 确保有时间戳
        now = datetime.now(timezone.utc)
        now_beijing = _format_beijing(now)
        
        if "created_at" not in task_data:
            task_data["created_at"] = now
            task_data["created_at_beijing"] = now_beijing
            
        if "status" in task_data and task_data["status"] == "completed" and "completed_at" not in task_data:
            task_data["completed_at"] = now
            task_data["completed_at_beijing"] = now_beijing
        
        # 确保视频ID在数据中
        if "video_id" not in task_data:
            task_data["video_id"] = video_id
            
        # 初始化请求计数和独立用户
        if "request_count" not in task_data:
            task_data["request_count"] = 1
            
        if "unique_users" not in task_data:
            user_id = task_data.get("user_id", "anonymous")
            task_data["unique_users"] = [user_id]
        
        # 保存到Firestore，使用video_id作为文档ID
        doc_ref = db.collection(TASKS_COLLECTION).document(video_id)
        existing_doc = doc_ref.get()
        
        if existing_doc.exists:
            # 如果记录已存在，更新相关字段
            existing_data = existing_doc.to_dict()
            
            # 更新请求计数
            task_data["request_count"] = existing_data.get("request_count", 0) + 1
            
            # 更新独立用户
            unique_users = set(existing_data.get("unique_users", []))
            user_id = task_data.get("user_id", "anonymous")
            if user_id not in unique_users:
                unique_users.add(user_id)
                task_data["unique_users"] = list(unique_users)
            else:
                task_data["unique_users"] = existing_data.get("unique_users", [])
                
            # 不覆盖创建时间
            if "created_at" in existing_data:
                task_data["created_at"] = existing_data["created_at"]
                task_data["created_at_beijing"] = existing_data.get("created_at_beijing", "")
            
            # 合并更新，保留原有字段
            doc_ref.set(task_data, merge=True)
        else:
            # 新建记录
            doc_ref.set(task_data)
            
        logger.info(f"任务 {task_id} 保存到Firestore成功")
        
        # 更新用户任务统计
        update_user_task_stats(task_data.get("user_id", "anonymous"), video_id, task_data)
        
        # 更新全局统计数据
        update_analytics_stats(video_id, task_data)
        
        return True
    except Exception as e:
        logger.error(f"保存任务 {task_id} 到Firestore失败: {str(e)}", exc_info=True)
        return False


def get_task(video_id: str) -> Optional[Dict[str, Any]]:
    """
    从Firestore获取任务数据
    
    参数:
        video_id: 视频ID
        
    返回:
        Dict[str, Any]: 任务数据，如不存在则返回None
    """
    try:
        doc_ref = db.collection(TASKS_COLLECTION).document(video_id)
        doc = doc_ref.get()
        if doc.exists:
            return doc.to_dict()
        return None
    except Exception as e:
        logger.error(f"获取任务 {video_id} 出错: {str(e)}", exc_info=True)
        return None


def update_task_status(task_id: str, status: str, progress: float = None, 
                      error: str = None, result_url: str = None) -> bool:
    """
    更新任务状态
    
    参数:
        task_id: 任务ID
        status: 任务状态
        progress: 进度 (0-1)
        error: 错误信息
        result_url: 结果URL
        
    返回:
        bool: 是否更新成功
    """
    try:
        # 获取任务数据
        doc_ref = db.collection(TASKS_COLLECTION).document(task_id)
        doc = doc_ref.get()
        
        if not doc.exists:
            logger.warning(f"更新状态失败: 任务 {task_id} 不存在")
            return False
            
        now = datetime.now(timezone.utc)
        now_beijing = get_beijing_time(now)
        
        update_data = {
            "status": status
        }
        
        if progress is not None:
            update_data["progress"] = progress
            
        if error is not None:
            update_data["error"] = error
            
        if result_url is not None:
            update_data["result_url"] = result_url
            
        # 如果状态为完成，添加完成时间
        if status == "completed":
            update_data["completed_at"] = now
            update_data["completed_at_beijing"] = now_beijing
            
        # 如果状态为失败，记录失败信息到统计数据
        if status == "failed" and error:
            task_data = doc.to_dict()
            video_id = task_data.get("video_id", task_id)
            user_id = task_data.get("user_id", "anonymous")
            video_title = task_data.get("video_title", "未知视频")
            
            # 记录失败信息到统计数据
            date_string = get_date_string(now)
            daily_stats_ref = db.collection(ANALYTICS_COLLECTION).document("daily_stats") \
                               .collection("dates").document(date_string)
            
            failed_video_details = {
                "video_id": video_id,
                "title": video_title,
                "error": error,
                "user_id": user_id,
                "failed_at": now,
                "failed_at_beijing": now_beijing
            }
            
            # 获取现有失败列表
            daily_doc = daily_stats_ref.get()
            if daily_doc.exists:
                daily_data = daily_doc.to_dict()
                failed_videos = daily_data.get("failed_video_details", [])
                failed_videos.append(failed_video_details)
                
                # 更新失败任务计数
                failed_count = daily_data.get("failed_tasks", 0) + 1
                
                daily_stats_ref.update({
                    "failed_tasks": failed_count,
                    "failed_video_details": failed_videos
                })
            else:
                # 创建新文档
                daily_stats_ref.set({
                    "failed_tasks": 1,
                    "failed_video_details": [failed_video_details]
                }, merge=True)
            
        doc_ref.update(update_data)
        logger.info(f"任务 {task_id} 状态更新为: {status}")
        return True
    except Exception as e:
        logger.error(f"更新任务 {task_id} 状态失败: {str(e)}", exc_info=True)
        return False


def get_user_tasks(user_id: str, limit: int = 10, 
                 last_doc_id: str = None, status_filter: str = None) -> List[Dict[str, Any]]:
    """
    获取用户的任务列表，支持分页和状态过滤
    以后用在dashboard页面，显示用户的翻译历史
    不对用户开发
    
    参数:
        user_id: 用户ID
        limit: 每页记录数
        last_doc_id: 上一页最后一条记录的ID
        status_filter: 状态过滤
        
    返回:
        List[Dict[str, Any]]: 任务列表
    """
    try:
        # 查询用户历史记录子集合
        user_ref = db.collection(USER_TASKS_COLLECTION).document(user_id)
        video_history_ref = user_ref.collection("video_history")
        
        # 创建基础查询，按最后请求时间倒序排序
        query = video_history_ref.order_by("last_requested_at", direction="DESCENDING")
        
        # 使用cursor进行分页
        if last_doc_id:
            last_doc = video_history_ref.document(last_doc_id).get()
            if last_doc.exists:
                query = query.start_after(last_doc)
        
        # 限制返回数量
        query = query.limit(limit)
        
        # 执行查询
        docs = query.stream()
        video_ids = []
        history_items = {}
        
        # 收集所有视频ID
        for doc in docs:
            video_id = doc.id
            video_ids.append(video_id)
            history_items[video_id] = doc.to_dict()
            
        if not video_ids:
            return []
            
        # 批量获取任务状态信息
        tasks_ref = db.collection(TASKS_COLLECTION)
        tasks = []
        
        # 查询每个视频的当前状态
        for video_id in video_ids:
            task_doc = tasks_ref.document(video_id).get()
            if task_doc.exists:
                task_data = task_doc.to_dict()
                
                # 如果有状态过滤且不匹配，则跳过
                if status_filter and task_data.get("status") != status_filter:
                    continue
                    
                # 合并历史记录信息
                history_data = history_items.get(video_id, {})
                task_data["first_requested_at"] = history_data.get("first_requested_at")
                task_data["first_requested_at_beijing"] = history_data.get("first_requested_at_beijing")
                task_data["last_requested_at"] = history_data.get("last_requested_at")
                task_data["last_requested_at_beijing"] = history_data.get("last_requested_at_beijing")
                task_data["user_request_count"] = history_data.get("request_count", 1)
                
                # 添加任务ID
                task_data["task_id"] = video_id
                task_data["video_id"] = video_id
                
                tasks.append(task_data)
            
        return tasks
    except Exception as e:
        logger.error(f"获取用户 {user_id} 的任务列表失败: {str(e)}", exc_info=True)
        return []


def update_user_task_stats(user_id: str, video_id: str, task_data: Dict[str, Any]) -> bool:
    """
    更新用户任务统计信息
    
    参数:
        user_id: 用户ID
        video_id: 视频ID
        task_data: 任务数据
        
    返回:
        bool: 是否更新成功
    """
    try:
        if not user_id or user_id == "anonymous":
            return True  # 匿名用户不更新统计
            
        now = datetime.now(timezone.utc)
        now_beijing = _format_beijing(now)
        date_string = get_date_string(now)
        
        # 获取用户文档
        user_ref = db.collection(USER_TASKS_COLLECTION).document(user_id)
        user_doc = user_ref.get()
        
        # 更新用户总体统计
        if user_doc.exists:
            user_data = user_doc.to_dict()
            total_requests = user_data.get("total_requests", 0) + 1
            
            # 检查最后活跃日期是否为今天
            last_active_date = user_data.get("last_active_date", "")
            
            user_ref.update({
                "total_requests": total_requests,
                "updated_at": now,
                "last_active_date": date_string
            })
        else:
            # 创建新用户记录
            user_ref.set({
                "email": task_data.get("email", ""),
                "display_name": task_data.get("display_name", ""),
                "total_requests": 1,
                "daily_limit": 3,  # 默认每日限额
                "created_at": now,
                "updated_at": now,
                "last_active_date": date_string
            })
            
        # 更新日统计子集合
        daily_ref = user_ref.collection("daily_usage").document(date_string)
        daily_doc = daily_ref.get()
        
        if daily_doc.exists:
            daily_data = daily_doc.to_dict()
            # 更新请求计数
            count = daily_data.get("count", 0) + 1
            # 添加视频ID到列表中
            videos = set(daily_data.get("videos", []))
            videos.add(video_id)
            
            daily_ref.update({
                "count": count,
                "videos": list(videos),
                "updated_at": now
            })
        else:
            # 创建新的日记录
            daily_ref.set({
                "count": 1,
                "videos": [video_id],
                "created_at": now,
                "updated_at": now
            })
            
        # 更新视频历史子集合
        video_history_ref = user_ref.collection("video_history").document(video_id)
        video_doc = video_history_ref.get()
        
        video_title = task_data.get("video_title", "未知视频")
        
        if video_doc.exists:
            video_data = video_doc.to_dict()
            request_count = video_data.get("request_count", 0) + 1
            
            video_history_ref.update({
                "last_requested_at": now,
                "last_requested_at_beijing": now_beijing,
                "video_title": video_title,
                "request_count": request_count
            })
        else:
            # 创建新的视频记录
            video_history_ref.set({
                "first_requested_at": now,
                "first_requested_at_beijing": now_beijing,
                "last_requested_at": now,
                "last_requested_at_beijing": now_beijing,
                "video_title": video_title,
                "request_count": 1
            })
            
        return True
    except Exception as e:
        logger.error(f"更新用户 {user_id} 的任务统计失败: {str(e)}", exc_info=True)
        return False


def update_analytics_stats(video_id: str, task_data: Dict[str, Any]) -> bool:
    """
    更新全局统计数据
    
    参数:
        video_id: 视频ID
        task_data: 任务数据
        
    返回:
        bool: 是否更新成功
    """
    try:
        now = datetime.now(timezone.utc)
        date_string = get_date_string(now)
        
        # 获取用户ID
        user_id = task_data.get("user_id", "anonymous")
        status = task_data.get("status", "pending")
        
        # 获取日统计记录
        daily_stats_ref = db.collection(ANALYTICS_COLLECTION).document("daily_stats") \
                           .collection("dates").document(date_string)
        daily_doc = daily_stats_ref.get()
        
        if daily_doc.exists:
            daily_data = daily_doc.to_dict()
            
            # 更新总请求数
            total_requests = daily_data.get("total_requests", 0) + 1
            
            # 更新独立用户数和列表
            unique_users_list = set(daily_data.get("unique_users_list", []))
            was_new_user = user_id not in unique_users_list
            
            if was_new_user and user_id != "anonymous":
                unique_users_list.add(user_id)
                
            # 更新新创建任务计数
            new_tasks = daily_data.get("new_tasks", 0)
            if task_data.get("request_count", 1) == 1:  # 第一次请求
                new_tasks += 1
                
            # 更新完成任务计数
            completed_tasks = daily_data.get("completed_tasks", 0)
            if status == "completed":
                completed_tasks += 1
                
            # 更新热门视频
            popular_videos = daily_data.get("popular_videos", [])
            video_title = task_data.get("video_title", "未知视频")
            
            # 查找视频是否在热门列表中
            video_found = False
            for i, video in enumerate(popular_videos):
                if video.get("video_id") == video_id:
                    # 更新计数
                    popular_videos[i]["count"] = video.get("count", 0) + 1
                    video_found = True
                    break
                    
            if not video_found:
                # 添加新视频到列表
                popular_videos.append({
                    "video_id": video_id,
                    "title": video_title,
                    "count": 1
                })
                
            # 按计数排序热门视频
            popular_videos.sort(key=lambda x: x.get("count", 0), reverse=True)
            
            # 仅保留前20个热门视频
            if len(popular_videos) > 20:
                popular_videos = popular_videos[:20]
                
            # 更新日统计数据
            daily_stats_ref.update({
                "total_requests": total_requests,
                "unique_users": len(unique_users_list),
                "unique_users_list": list(unique_users_list),
                "new_tasks": new_tasks,
                "completed_tasks": completed_tasks,
                "popular_videos": popular_videos
            })
        else:
            # 创建新的日统计记录
            unique_users_list = [user_id] if user_id != "anonymous" else []
            
            # 确定任务状态相关计数
            new_tasks = 1 if task_data.get("request_count", 1) == 1 else 0
            completed_tasks = 1 if status == "completed" else 0
            failed_tasks = 1 if status == "failed" else 0
            
            # 初始化热门视频列表
            video_title = task_data.get("video_title", "未知视频")
            popular_videos = [{
                "video_id": video_id,
                "title": video_title,
                "count": 1
            }]
            
            # 创建日统计记录
            daily_stats_ref.set({
                "total_requests": 1,
                "unique_users": len(unique_users_list),
                "unique_users_list": unique_users_list,
                "new_tasks": new_tasks,
                "completed_tasks": completed_tasks,
                "failed_tasks": failed_tasks,
                "popular_videos": popular_videos
            })
            
        return True
    except Exception as e:
        logger.error(f"更新统计数据失败: {str(e)}", exc_info=True)
        return False


# def check_user_daily_limit(user_id: str) -> Dict[str, Any]:
#     """
#     检查用户是否超过每日限额
    
#     参数:
#         user_id: 用户ID
        
#     返回:
#         Dict: 包含以下字段:
#             - has_limit: 是否有限制
#             - limit_exceeded: 是否超过限额
#             - daily_limit: 每日限额
#             - used_today: 今日已用次数
#             - remaining: 剩余次数
#     """
#     try:
#         if not user_id or user_id == "anonymous":
#             # 匿名用户不限制
#             return {
#                 "has_limit": False,
#                 "limit_exceeded": False,
#                 "daily_limit": 0,
#                 "used_today": 0,
#                 "remaining": 0
#             }
            
#         date_string = get_date_string()
        
#         # 获取用户信息
#         user_ref = db.collection(USER_TASKS_COLLECTION).document(user_id)
#         user_doc = user_ref.get()
        
#         if not user_doc.exists:
#             # 新用户，设置默认限额
#             return {
#                 "has_limit": True,
#                 "limit_exceeded": False,
#                 "daily_limit": 3,  # 默认限额
#                 "used_today": 0,
#                 "remaining": 3
#             }
            
#         user_data = user_doc.to_dict()
#         daily_limit = user_data.get("daily_limit", 3)
        
#         # 如果用户没有限额
#         if daily_limit <= 0:
#             return {
#                 "has_limit": False,
#                 "limit_exceeded": False,
#                 "daily_limit": 0,
#                 "used_today": 0,
#                 "remaining": 0
#             }
            
#         # 获取今日使用情况
#         daily_ref = user_ref.collection("daily_usage").document(date_string)
#         daily_doc = daily_ref.get()
        
#         if not daily_doc.exists:
#             # 今日未使用
#             return {
#                 "has_limit": True,
#                 "limit_exceeded": False,
#                 "daily_limit": daily_limit,
#                 "used_today": 0,
#                 "remaining": daily_limit
#             }
            
#         daily_data = daily_doc.to_dict()
#         used_today = daily_data.get("count", 0)
#         remaining = max(0, daily_limit - used_today)
        
#         return {
#             "has_limit": True,
#             "limit_exceeded": used_today >= daily_limit,
#             "daily_limit": daily_limit,
#             "used_today": used_today,
#             "remaining": remaining
#         }
#     except Exception as e:
#         logger.error(f"检查用户 {user_id} 每日限额失败: {str(e)}", exc_info=True)
#         # 失败时不限制用户
#         return {
#             "has_limit": False,
#             "limit_exceeded": False,
#             "daily_limit": 0,
#             "used_today": 0,
#             "remaining": 0
#         }


def count_user_tasks(user_id: str, status_filter: str = None) -> int:
    """
    计算用户任务数量
    
    参数:
        user_id: 用户ID
        status_filter: 状态过滤
        
    返回:
        int: 任务数量
    """
    try:
        # 获取用户视频历史子集合
        user_ref = db.collection(USER_TASKS_COLLECTION).document(user_id)
        video_history_ref = user_ref.collection("video_history")
        
        # 计算用户历史记录总数
        history_count = len(list(video_history_ref.stream()))
        
        # 如果没有状态过滤，直接返回历史记录数
        if not status_filter:
            return history_count
            
        # 如果有状态过滤，需要检查每个视频的当前状态
        history_docs = video_history_ref.stream()
        count = 0
        
        for doc in history_docs:
            video_id = doc.id
            # 获取该视频的当前状态
            task_doc = db.collection(TASKS_COLLECTION).document(video_id).get()
            if task_doc.exists:
                task_data = task_doc.to_dict()
                if task_data.get("status") == status_filter:
                    count += 1
                    
        return count
    except Exception as e:
        logger.error(f"计算用户 {user_id} 的任务数量失败: {str(e)}", exc_info=True)
        return 0 
    
#0523 视频信息、用户信息、任务信息 3部分数据操作代码

# 集合名称常量
VIDEOINFO_COLLECTION = "videoinfo"
USERINFO_COLLECTION = "userinfo"
DAILY_SUBCOL = "daily_usage"
HISTORY_SUBCOL = "video_history"
USER_TASK_COLLECTION = "user_task"


def _format_beijing_time(dt: datetime) -> str:
    """将任意时间（UTC 或本地）转换为北京时间格式字符串"""
    bj_tz = pytz.timezone("Asia/Shanghai")
    
    # 如果 dt 是 naive datetime（没有 tzinfo），默认它是 UTC
    if dt.tzinfo is None:
        dt = pytz.UTC.localize(dt)
    
    # 转换为北京时间
    dt_bj = dt.astimezone(bj_tz)
    
    return dt_bj.strftime("%Y-%m-%d %H:%M:%S")


def _video_ref(video_id: str):
    """获取视频任务文档引用"""
    return db.collection(VIDEOINFO_COLLECTION).document(video_id)


def _user_ref(user_id: str):
    """获取用户主文档引用"""
    return db.collection(USERINFO_COLLECTION).document(user_id)


def _daily_ref(user_id: str, date_str: str):
    """获取用户当日使用量子集合文档引用"""
    return _user_ref(user_id).collection(DAILY_SUBCOL).document(date_str)


def _history_ref(user_id: str, video_id: str):
    """获取用户视频历史子集合文档引用"""
    return _user_ref(user_id).collection(HISTORY_SUBCOL).document(video_id)

def _user_task_ref(task_id: str):
    """获取用户任务文档引用"""
    return db.collection(USER_TASK_COLLECTION).document(task_id)


def create_or_update_video_task(
    video_id: str,
    youtube_url: str,
    video_title: str,
    user_id: str,
    translation_strategies: Optional[List[str]] = None
) -> None:
    """
    创建或更新视频任务文档：
      - 已存在：请求计数自增、去重添加用户
      - 不存在：初始化所有字段，包括状态、进度、时间戳等
    """
    strategies = translation_strategies or []
    ref = _video_ref(video_id)
    doc = ref.get()
    now = datetime.now(timezone.utc)

    if doc.exists:
        # 文档已存在，原子增加计数并合并用户列表
        ref.update({
            "request_count": Increment(1),
            "unique_users": ArrayUnion([user_id])
        })
    else:
        # 新文档初始化所有字段
        ref.set({
            "video_id": video_id,
            "youtube_url": youtube_url,
            "video_title": video_title,
            "status": "processing",       # 任务状态
            "progress": 0.1,             # 任务进度
            "translation_strategies": strategies,
            "request_count": 1,
            "unique_users": [user_id],
            "asr_url": "",             # 转写结果 URL
            "result_url": "",          # 翻译结果 URL
            "error": "",               # 错误信息
            "created_at": SERVER_TIMESTAMP,
            "created_at_beijing": _format_beijing_time(now)
        })


def update_video_task(
    video_id: str,
    status: str,
    progress: Optional[float] = None,
    translation_strategies: Optional[List[str]] = None,
    asr_url: Optional[str] = None,
    result_url: Optional[str] = None,
    error: Optional[str] = None
) -> None:
    """
    更新视频任务的状态或其他字段，并记录更新时间：
      - 状态、进度、策略、URL、错误等可选更新
    """
    ref = _video_ref(video_id)
    now = datetime.now(timezone.utc)
    updates: Dict[str, Any] = {
        "status": status,                           # 新的任务状态
        "updated_at": SERVER_TIMESTAMP,
        "updated_at_beijing": _format_beijing_time(now)
    }
    # 根据是否提供参数，动态添加更新字段
    if progress is not None:
        updates["progress"] = progress
    if translation_strategies is not None:
        updates["translation_strategies"] = translation_strategies
    if asr_url is not None:
        updates["asr_url"] = asr_url
    if result_url is not None:
        updates["result_url"] = result_url
    if error is not None:
        updates["error"] = error

    ref.update(updates)


def get_video_task(video_id: str) -> Optional[Dict[str, Any]]:
    """获取视频任务文档，返回字典或 None"""
    snap = _video_ref(video_id).get()
    return snap.to_dict() if snap.exists else None

# def get_video_task(video_id: str) -> Optional[Dict[str, Any]]:
#     """获取视频任务文档，返回字典或 None，并且注入 task_id"""
#     snap = _video_ref(video_id).get()
#     if not snap.exists:
#         return None
#     data = snap.to_dict()
#     # 把文档的 ID (也就是 task_id) 加进来
#     #data["task_id"] = snap.id
#     return data


def ensure_user(user_id: str, email: str) -> None:
    """
    创建或更新用户主文档：
      - 新用户：初始化 email、总请求数、每日限额、创建/更新时间
      - 老用户：仅更新活跃时间 updated_at
    """
    ref = _user_ref(email)
    doc = ref.get()
    payload = {"updated_at": SERVER_TIMESTAMP}
    if not doc.exists:
        payload.update({
            "user_id": user_id,
            "total_requests": 0,
            "daily_limit": DEFAULT_DAILY_LIMIT,
            "created_at": SERVER_TIMESTAMP
        })
        ref.set(payload, merge=True)
    else:
        ref.update(payload)


def record_request(user_id: str, video_id: str, video_title: str) -> None:
    """
    在事务中：
      1. 校验当天请求是否超限
      2. 更新 daily_usage、total_requests
      3. 更新或创建 video_history
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    user_ref = _user_ref(user_id)
    daily_ref = _daily_ref(user_id, today)
    history_ref = _history_ref(user_id, video_id)

    def txn_fn(txn):
        user_doc = txn.get(user_ref)
        if not user_doc.exists:
            raise NotFound(f"User {user_id} not found")

        # 获取今日用量并递增
        prev_daily = txn.get(daily_ref)
        count = (prev_daily.get("count") if prev_daily.exists else 0) + 1
        if count > user_doc.get("daily_limit"):
            raise Exception("超出每日限制")

        videos = prev_daily.get("videos", []) if prev_daily.exists else []
        videos.append(video_id)

        # 合并设置每日统计
        txn.set(daily_ref, {
            "count": count,
            "videos": videos,
            "date_timestamp": datetime.now(timezone.utc),
            "updated_at": SERVER_TIMESTAMP
        }, merge=True)

        # 增加用户总请求数
        txn.update(user_ref, {"total_requests": Increment(1), "updated_at": SERVER_TIMESTAMP})

        # 更新或创建视频历史记录
        hist = txn.get(history_ref)
        if hist.exists:
            txn.update(history_ref, {
                "last_requested_at": SERVER_TIMESTAMP,
                "request_count": Increment(1)
            })
        else:
            txn.set(history_ref, {
                "video_title": video_title,
                "first_requested_at": SERVER_TIMESTAMP,
                "last_requested_at": SERVER_TIMESTAMP,
                "request_count": 1
            })

    # 执行事务
    db.run_transaction(txn_fn)

def check_user_daily_limit(user_id: str) -> bool:
    """
    检查用户是否超出每日请求限制（不做任何写操作）
    """
    # 1. 准备"今天"的日期串
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # 2. 读用户主文档
    user_snap = _user_ref(user_id).get()
    if not user_snap.exists:
        raise NotFound(f"用户不存在：{user_id}")
    user_data   = user_snap.to_dict() or {}
    daily_limit = user_data.get("daily_limit", 3)

    # 3. 读当天的子文档
    daily_snap = _daily_ref(user_id, today).get()
    if not daily_snap.exists:
        count = 0
    else:
        # 转成 dict，再用 dict.get
        daily_data = daily_snap.to_dict() or {}
        count      = daily_data.get("count", 0)

    # 4. 判断是否超限
    return count < daily_limit


def get_video_id_from_task(task_id: str) -> str:
    """
    根据 task_id 从 user_task 表中获取对应的 video_id
    """
    # 1. 获取 user_task 文档快照
    snapshot = _user_task_ref(task_id).get()

    # 2. 判断文档是否存在
    if not snapshot.exists:
        raise HTTPException(status_code=404, detail=f"任务 {task_id} 不存在")

    # 3. 转为字典并校验字段
    data = snapshot.to_dict()
    if not data or "video_id" not in data:
        raise HTTPException(
            status_code=500,
            detail=f"任务 {task_id} 数据异常：缺少 video_id 字段"
        )

    # 4. 返回 video_id
    return data["video_id"]


def get_user_limit_info(user_id: str) -> Dict[str, Any]:
    """
    获取用户每日请求限制信息
    """
    # 1. 准备"今天"的日期串
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # 2. 读用户主文档获取每日上限
    user_data = _user_ref(user_id).get().to_dict() or {}
    daily_limit = user_data.get("daily_limit", DEFAULT_DAILY_LIMIT)

    # 3. 读当天的子文档获取当日使用量
    daily_data = _daily_ref(user_id, today).get().to_dict() or {}
    used_today = daily_data.get("count", 0)

    return {
        "daily_limit": daily_limit,
        "used_today": used_today
    }

def record_successful_request(user_id: str, video_id: str, video_title: str) -> None:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    user_ref = _user_ref(user_id)
    daily_ref = _daily_ref(user_id, today)
    history_ref = _history_ref(user_id, video_id)

    @firestore.transactional
    def txn_fn(transaction):
        # 1. 先读取所有文档
        user_doc   = next(transaction.get(user_ref))
        prev_daily = next(transaction.get(daily_ref))
        hist       = next(transaction.get(history_ref))

        # 2. 从 snapshot 转为 dict，再安全取值
        daily_data = prev_daily.to_dict() or {}
        count      = daily_data.get("count", 0) + 1
        videos     = daily_data.get("videos", [])
        if video_id not in videos:
            videos.append(video_id)

        # 3. 开始写入操作
        transaction.set(
            daily_ref,
            {
                "count": count,
                "videos": videos,
                "date_timestamp": datetime.utcnow(),
                "updated_at": SERVER_TIMESTAMP,
            },
            merge=True,
        )
        transaction.update(
            user_ref,
            {"total_requests": Increment(1), "updated_at": SERVER_TIMESTAMP},
        )

        if hist.exists:
            transaction.update(
                history_ref,
                {
                    "last_requested_at": SERVER_TIMESTAMP,
                    "request_count": Increment(1),
                },
            )
        else:
            transaction.set(
                history_ref,
                {
                    "video_title": video_title,
                    "first_requested_at": SERVER_TIMESTAMP,
                    "last_requested_at": SERVER_TIMESTAMP,
                    "request_count": 1,
                },
            )

    txn = db.transaction()
    txn_fn(txn)


def create_user_task(
    user_id: str,
    video_id: str,
    youtube_url: str,
    task_id: str,
    is_new: bool
) -> None:
    """
    在 user_task 集合中记录用户任务
    """
    now = datetime.now(timezone.utc)
    db.collection(USER_TASK_COLLECTION).document(task_id).set({
        "user_id": user_id,
        "video_id": video_id,
        "youtube_url": youtube_url,
        "task_id": task_id,
        "is_new": is_new,
        "created_at": SERVER_TIMESTAMP,
        "created_at_beijing": _format_beijing_time(now)
    })