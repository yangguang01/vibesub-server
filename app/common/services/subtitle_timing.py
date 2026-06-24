import datetime
import re

from app.common.core.logging import logger


def format_time(seconds):
    """将秒数转换为 SRT 格式的时间字符串，格式为 hh:mm:ss,mmm"""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    milliseconds = int(round((seconds - int(seconds)) * 1000))
    return f"{hours:02}:{minutes:02}:{secs:02},{milliseconds:03}"


def subtitles_to_dict(subtitles):
    """
    Parse subtitles that include a number, a time range, and text.
    Returns a dictionary with numbers as keys and a tuple (time range, text) as values.
    """
    subtitles_dict = {}
    lines = subtitles.strip().split("\n")
    current_number = None
    current_time_range = ""
    current_text = ""

    for line in lines:
        if line.isdigit():
            if current_number is not None:
                subtitles_dict[current_number] = (current_time_range, current_text.strip())
            current_number = int(line)
        elif '-->' in line:
            current_time_range = line
            current_text = ""
        else:
            current_text += line + " "

    subtitles_dict[current_number] = (current_time_range, current_text.strip())

    return subtitles_dict


def map_marged_sentence_to_timeranges(merged_content, subtitles):
    """
    For each merged sentence, find the corresponding subtitles and their time ranges by concatenating
    the subtitles sentences until they match the merged sentence, and merge the time ranges accordingly.
    This version correctly handles multiple merged sentences.
    """
    merged_to_subtitles = {}
    subtitle_index = 0  # Keep track of the current position in the subtitles

    for num, merged_sentence in merged_content.items():
        corresponding_subtitles = []
        start_time = None
        end_time = None
        temp_sentence = ""

        while subtitle_index < len(subtitles):
            sub_num, (time_range, subtitle) = list(subtitles.items())[subtitle_index]
            if start_time is None:
                start_time = time_range.split(' --> ')[0]  # Set the start time of the first subtitle

            temp_sentence += subtitle + " "
            end_time = time_range.split(' --> ')[1]  # Update the end time with each subtitle added
            corresponding_subtitles.append(subtitle)

            # Check if the concatenated subtitles match the merged sentence
            if temp_sentence.strip() == merged_sentence:
                merged_time_range = f"{start_time} --> {end_time}"
                merged_to_subtitles[num] = (merged_time_range, temp_sentence)
                subtitle_index += 1  # Move to the next subtitle for the next iteration
                break

            subtitle_index += 1

    return merged_to_subtitles


def map_chinese_to_time_ranges_v2(chinese_content, merged_engsentence_to_subtitles):
    """
    给中文翻译添加时间轴，生成未经句子长度优化的初始中文字幕。

    参数:
        chinese_content (dict): 字典，key 为编号，value 为中文翻译字符串。
        merged_engsentence_to_subtitles (dict): 字典，key 为编号，value 为一个元组，格式为 (time_range, subtitle)。

    返回:
        dict: key 为编号，value 为一个字典，包含以下键:
              - "time_range": 原始时间区间字符串
              - "text": 对应的中文翻译
    """
    chinese_to_time = {}

    for num, chinese_sentence in chinese_content.items():
        # 如果当前编号在英文字幕合并结果中存在
        if num in merged_engsentence_to_subtitles:
            time_range, _ = merged_engsentence_to_subtitles[num]
            # 用自描述的字典结构保存信息
            chinese_to_time[num] = {
                "time_range": time_range,
                "text": chinese_sentence
            }

    # 防御性日志：修复 P000 后，正常情况下每行中文都应能匹配到时间轴。
    # 若仍有中文行被丢弃，说明上游行号与时间轴字典不一致，记日志以免静默丢行。
    if len(chinese_to_time) != len(chinese_content):
        dropped = sorted(set(chinese_content) - set(chinese_to_time))
        logger.warning(
            f"[时间轴映射] {len(dropped)} 行中文未匹配到时间轴被丢弃: {dropped[:30]}"
        )

    return chinese_to_time


def time_to_str(dt):
    """
    将 datetime 对象格式化为 SRT 字幕时间格式：HH:MM:SS,mmm
    """
    return dt.strftime("%H:%M:%S,%f")[:-3]


def format_subtitles_v2(subtitles_dict):
    formatted_str = ""
    num_counter = 1  # 初始化计数器
    for key in sorted(subtitles_dict.keys()):
        subtitle = subtitles_dict[key]
        formatted_str += f"{num_counter}\n"
        formatted_str += f"{subtitle['time_range']}\n"
        formatted_str += f"{subtitle['text']}\n\n"
        num_counter += 1
    return formatted_str


def parse_time_range(time_range_str):
    """
    解析形如 "HH:MM:SS,mmm --> HH:MM:SS,mmm" 的时间区间字符串，
    返回起始时间和结束时间对应的 datetime 对象。
    此处以 1900-01-01 为基础日期。
    """
    try:
        start_str, end_str = time_range_str.split(" --> ")
        base_date = datetime.date(1900, 1, 1)
        start_dt = datetime.datetime.strptime(f"{base_date} {start_str}", "%Y-%m-%d %H:%M:%S,%f")
        end_dt = datetime.datetime.strptime(f"{base_date} {end_str}", "%Y-%m-%d %H:%M:%S,%f")
        return start_dt, end_dt
    except Exception as e:
        logger.error(f"解析时间范围错误: {time_range_str}, 错误: {str(e)}")
        raise
