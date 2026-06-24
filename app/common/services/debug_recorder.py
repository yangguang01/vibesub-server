import datetime
import threading

from app.common.core.logging import logger


# 全局调试记录存储
debug_records = []
debug_lock = threading.Lock()


def add_debug_record(video_id, chunk_info, input_data, output_data, result_info, attempt_type="initial"):
    """
    添加调试记录到全局列表

    Args:
        video_id: 视频ID
        chunk_info: 块信息 (first_item_number, end_item_number, expected_lines)
        input_data: 输入数据 (system_prompt, user_content)
        output_data: 输出数据 (raw_response, parsed_json, actual_lines)
        result_info: 结果信息 (success, line_count_match, error_message)
        attempt_type: 尝试类型 ("initial", "retry")
    """
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    record = {
        'timestamp': timestamp,
        'video_id': video_id,
        'chunk_first': chunk_info['first'],
        'chunk_end': chunk_info['end'],
        'attempt_type': attempt_type,
        'content': f"""[{timestamp}] VIDEO: {video_id} | CHUNK: {chunk_info['first']}-{chunk_info['end']} | ATTEMPT: {attempt_type}
INPUT_LINES: {chunk_info['expected']}
{input_data['user_content']}
---
OUTPUT_LINES: {output_data.get('actual_lines', 'ERROR')} (Expected: {chunk_info['expected']})
{output_data.get('formatted_output', 'ERROR')}
---
RESULT: {'SUCCESS' if result_info['success'] else 'FAILED'} ({chunk_info['expected']}→{output_data.get('actual_lines', '?')}) {result_info.get('error_message', '')}
{'='*80}
"""
    }

    with debug_lock:
        debug_records.append(record)


def get_debug_records_text():
    """
    获取所有调试记录的文本内容，按CHUNK编号排序

    Returns:
        str: 格式化的调试记录文本
    """
    with debug_lock:
        if not debug_records:
            return "No debug records found.\n"

        # 按CHUNK编号排序：先按chunk_first，再按attempt_type (initial在前，retry在后)
        sorted_records = sorted(debug_records, key=lambda x: (
            x['chunk_first'],
            0 if x['attempt_type'] == 'initial' else 1,  # initial排在retry前面
            x['timestamp']
        ))

        header = f"""=== LLM Translation Debug Records ===
Generated: {datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
Total Records: {len(debug_records)}
Sorted by: CHUNK number, then attempt type (initial → retry)
{'='*80}

"""
        return header + '\n'.join(record['content'] for record in sorted_records)


def clear_debug_records():
    """清空调试记录"""
    with debug_lock:
        debug_records.clear()


async def save_debug_records_to_storage(video_id, bucket):
    """
    将调试记录保存到 Firebase Storage

    Args:
        video_id: 视频ID
        bucket: Firebase Storage bucket 实例

    Returns:
        str: 上传后的文件URL，如果没有记录则返回None
    """
    try:
        debug_text = get_debug_records_text()

        # 如果没有调试记录，直接返回
        if debug_text.strip() == "No debug records found.":
            logger.info("没有调试记录需要保存")
            return None

        # 上传到 Firebase Storage
        debug_blob = bucket.blob(f"debug_records/{video_id}_translation_debug.txt")
        debug_blob.upload_from_string(
            debug_text,
            content_type="text/plain; charset=utf-8"
        )

        logger.info(f"调试记录已保存到 Firebase Storage: debug_records/{video_id}_translation_debug.txt")
        return debug_blob.public_url

    except Exception as e:
        logger.error(f"保存调试记录到 Firebase Storage 失败: {str(e)}", exc_info=True)
        return None
