# -*- coding: utf-8 -*-
"""
校园跑 / 阳光跑 数据自动上传脚本（自用版）

来源声明
--------
本脚本的协议思路（如何抓包、如何构造登录与上传请求）来源于 GitHub 上
一位原作者公开的开源项目。本版本是在其基础上抄改得到的，只是做了一个
更"傻瓜化"的封装：学校代码写死、学号与里程可配置、写死学号后可直接全自动运行。
由于没能找到原作者仓库，这里先上传一个自用版本。如原作者本人要求下架，
请提交 Issue 联系，我会立即删除。

严禁任何商业用途。使用后果自负。详见 README.md。

技术栈：Python 3.8+ / requests
"""

import datetime
import gzip
import hashlib
import json
import math
import random
import re
import time

import requests

# ============================================================
# ① 配置区：一般只需要改这里
# ============================================================

# 学校代码：写死在代码里。10032 = 北京语言大学
SCHOOL_NO = "10032"

# 学号：填上就全自动，启动后不再需要手输；留空则启动时提示输入
STUDENT_NO = ""            # 例如 "202400000000"

# 单次跑步距离（米）
#   填数字（如 4000）：每次都跑这个固定距离
#   填 0            ：每次在 DISTANCE_RANGE 里随机，更像真人（推荐）
#   填 None         ：启动时手动输入
RUN_DISTANCE_M = 0

# 自动生成公里数的随机区间（米）。单次上限约 4km，别设太大
DISTANCE_RANGE = (3900, 4100)

# 启动模式：0 = 启动时询问；1 = 只跑一次（佛系）；2 = 穿越时间补到目标总里程（暴走）
AUTO_MODE = 0

# 暴走模式的目标总里程（公里）
TARGET_TOTAL_KM = 100.0

# 暴走模式从多少天前开始伪造日期（服务端信任客户端上报的时间戳，因此可以往前补）
TIME_TRAVEL_START_DAYS = 365

# 伪造轨迹的起点坐标（学校附近即可，默认北京语言大学东门一带）
START_LAT = 39.994356
START_LNG = 116.344145

# 折返跑：True = 跑到一半掉头，最后回到起点附近（更像真人）
LOOP_BACK = True

# 每次伪造跑步的配速范围（秒 / 公里），在该区间内随机取值
# 330~480 秒/公里 = 5'30" ~ 8'00"，约 7.5~10.9 km/h，学生慢跑的合理区间
PACE_RANGE = (330, 480)

# 单次请求超时（秒）
TIMEOUT = 20

# ============================================================
# ② 接口常量（换学校一般也要改 BASE_URL）
# ============================================================

BASE_URL = "http://222.28.94.116:8030/DragonFlyServ"
UPLOAD_INTERVAL = 4        # 轨迹点间隔（秒），服务端按该间隔还原轨迹


# ============================================================
# ②.5 公里数：固定 / 自动随机 / 手动输入
# ============================================================

def pick_distance(prompt_if_none=True):
    """按配置决定本次跑步的距离（米）。

    这是"公里数自动生成"的入口：
      RUN_DISTANCE_M > 0   -> 固定值
      RUN_DISTANCE_M == 0  -> DISTANCE_RANGE 里随机
      RUN_DISTANCE_M is None -> 问用户（直接回车也走随机）
    """
    if RUN_DISTANCE_M is None:
        if prompt_if_none:
            raw = input("请输入本次里程（米，如 4000；直接回车 = 自动随机）: ").strip()
            if raw:
                return float(raw)
    elif float(RUN_DISTANCE_M) > 0:
        return float(RUN_DISTANCE_M)
    return round(random.uniform(*DISTANCE_RANGE), 2)


# ============================================================
# ③ 底层请求
# ============================================================

def _post_gzip(url, payload):
    """把 dict 压成 gzip 直接作为请求体 POST（服务端自行解压）。"""
    body = gzip.compress(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    return requests.post(url, data=body, timeout=TIMEOUT)


def login(stuno, password, school):
    """登录并拿到 uid。App 的默认密码就是 md5(学号)。"""
    url = f"{BASE_URL}/DflyServer"

    # 第一步：绑定（原流程中的预处理请求，返回值不参与后续判断）
    try:
        requests.post(
            url,
            data={"name": f"['bangding', '{school}', 'student', '{stuno}', '{password}']"},
            timeout=TIMEOUT,
        )
    except requests.RequestException as e:
        print(f"[!] 绑定请求失败: {e}")

    # 第二步：本地绑定登录，响应形如 "xxx,<uid>"
    data = {
        "name": f"['bangdingschool_local','{school}','{stuno}','{password}',"
                f"'HarmonyOS','114514191','android','16']"
    }
    try:
        response = requests.post(url, data=data, timeout=TIMEOUT).text
    except requests.RequestException as e:
        print(f"[!] 登录请求失败: {e}")
        return None

    index = response.find(",")
    if index == -1:
        print(f"[!] 登录失败，请检查学号 / 学校代码 / 网络。服务端返回: {response[:200]}")
        return None

    uid = response[index + 1:].strip()
    print(f"[*] 登录成功，获取到 UID: {uid}")
    return uid


def getinfo(stuno, uid, school):
    """查询跑步数据汇总（返回原始文本）。"""
    url = f"{BASE_URL}/Api/webserver/getRunDataSummary"
    payload = {"studentno": stuno, "uid": uid, "schoolno": str(school)}
    return _post_gzip(url, payload).text


def get_real_distance(stuno, uid, school):
    """调用 getinfo 并提取真实的已跑公里数；失败返回 -1.0。"""
    try:
        info_text = getinfo(stuno, uid, school)

        # 服务端返回里带一句 "校内定向跑:15.915公里"
        match = re.search(r"校内定向跑:([\d\.]+)公里", str(info_text))
        if match:
            return float(match.group(1))

        # 兜底一：按 JSON 结构找 distance 字段
        try:
            data = json.loads(info_text)
            if "distance" in data:
                return float(data["distance"])
            if "data" in data and "distance" in data["data"]:
                return float(data["data"]["distance"])
        except (ValueError, TypeError):
            pass

        # 兜底二：粗暴取返回值里最大的浮点数
        nums = re.findall(r"\d+\.\d+", str(info_text))
        if nums:
            return max(float(n) for n in nums)
    except Exception as e:  # noqa: BLE001 - 查询失败不应中断主流程
        print(f"[!] 提取里程失败: {e}")
    return -1.0


# ============================================================
# ④ 核心：伪造轨迹 + 上传
# ============================================================

def generate_mock_path(start_lat, start_lng, total_time_sec, avg_speed, end_timestamp):
    """生成一条假的跑步轨迹。

    服务端要求的 location 字段格式：
        lat,lng;null;null;null;null;null      # 普通轨迹点
        ...
        @888,888;<结束时间戳>;null;null;null;null   # 结尾哨兵点
    所有点用 @ 连接，相邻点之间相隔 UPLOAD_INTERVAL 秒。

    path 字段只是把轨迹里抽 4 个点出来，用 ; 连起来。
    """
    interval = UPLOAD_INTERVAL
    num_points = max(2, int(total_time_sec) // interval)

    loc_list = []
    direction = random.uniform(0, 2 * math.pi)
    curr_lat, curr_lng = start_lat, start_lng

    for i in range(num_points):
        # 折返：跑到一半掉头，后半程往回跑，终点会回到起点附近
        if LOOP_BACK and i == num_points // 2:
            direction += math.pi

        drift_lat = random.uniform(-0.00001, 0.00001)
        drift_lng = random.uniform(-0.00001, 0.00001)

        step = avg_speed * interval
        curr_lat += (step * math.cos(direction)) / 111000 + drift_lat
        curr_lng += (step * math.sin(direction)) / (
            111000 * math.cos(math.radians(curr_lat))
        ) + drift_lng

        # 每隔 10 个点轻微拐一次弯，避免轨迹是一条数学意义上的直线
        if i % 10 == 0:
            direction += random.uniform(-0.1, 0.1)

        loc_list.append(f"{curr_lat:.15f},{curr_lng:.15f};null;null;null;null;null")

    # 结尾哨兵点：888,888 + 本次结束时间戳。服务端用它判断"跑完了没有"
    tail = f"888,888;{end_timestamp};null;null;null;null"
    location_str = "@".join(loc_list) + "@" + tail

    indices = [0, len(loc_list) // 3, 2 * len(loc_list) // 3, len(loc_list) - 1]
    path_points = [
        f"{loc_list[idx].split(',')[0]},{loc_list[idx].split(',')[1].split(';')[0]}"
        for idx in indices
    ]
    path_str = ";".join(path_points) + ";"

    return path_str, location_str


def upload_run(studentno, school, uid, target_distance_m, end_ts=None):
    """上传一条伪造的跑步记录。

    end_ts 为 None 时使用当前时间；传入任意历史时间戳即可"穿越"到那天补跑。
    """
    url = f"{BASE_URL}/Api/webserver/uploadRunData"

    pace_per_km = random.randint(*PACE_RANGE)
    total_time_sec = max(1, int((target_distance_m / 1000.0) * pace_per_km))
    avg_speed = round(target_distance_m / total_time_sec, 2)   # 单位：米/秒

    if end_ts is None:
        end_ts = int(time.time())
        print(f"[*] 正常发包时间: 当前时间 | 距离: {target_distance_m}m")
    else:
        end_ts = int(end_ts)
        stamp = datetime.datetime.fromtimestamp(end_ts).strftime("%Y-%m-%d %H:%M:%S")
        print(f"[*] 伪造发包时间: {stamp} | 距离: {target_distance_m}m")

    begin_ts = end_ts - total_time_sec
    path_data, location_data = generate_mock_path(
        START_LAT, START_LNG, total_time_sec, avg_speed, end_ts
    )
    point_count = len(location_data.split("@")) - 1
    print(f"[*] 轨迹已自动生成: {point_count} 个点 / 耗时 {total_time_sec}s / 配速 {pace_per_km}s每公里")

    payload = {
        "begintime": str(begin_ts),
        "endtime": str(end_ts),
        "uid": uid,
        "schoolno": str(school),
        "distance": str(float(target_distance_m)),
        "speed": str(avg_speed),
        "studentno": studentno,
        "atttype": "3",
        "eventno": "802",
        "location": location_data,
        "pointstatus": "1",
        "usetime": str(total_time_sec),
        "path": path_data,
        "ruleid": "3",
    }

    try:
        response = _post_gzip(url, payload)
    except requests.RequestException as e:
        print(f"[!] 上传请求失败: {e}")
        return False

    text = response.text.strip()
    if not text or "error" in text.lower() or "fail" in text.lower():
        print(f"[!] 服务端返回异常: {text[:200]}")
        return False

    return True


# ============================================================
# ⑤ 两种运行模式
# ============================================================

def run_once(stuno, school, uid):
    """佛系模式：只上传一条当前时间的记录。"""
    print("\n--- 启动：正常单次打卡 ---")
    dist = pick_distance()

    if upload_run(stuno, school, uid, dist):
        print("[+] 打卡成功！")
    else:
        print("[-] 打卡失败。")

    time.sleep(2)
    new_km = get_real_distance(stuno, uid, school)
    if new_km >= 0:
        print(f"[*] 最新总里程已更新为: {new_km} km")


def run_time_travel(stuno, school, uid, current_km, target_km):
    """暴走模式：从 N 天前开始，一天一天往前补，直到总里程达标。

    服务端直接信任客户端上报的 begintime / endtime，所以同一个"今天"
    可以无限补历史记录 —— 这就是能一天刷超过 4km 的原因。
    """
    print(f"\n--- 启动：暴走自动补全模式（当前 {current_km}km -> 目标 {target_km}km）---")

    fake_date = datetime.datetime.now() - datetime.timedelta(days=TIME_TRAVEL_START_DAYS)
    fail_count = 0

    while current_km < target_km:
        # 单次距离在 DISTANCE_RANGE 里随机，避免每次数据一模一样
        step_dist = pick_distance(prompt_if_none=False)

        # 当天的时间点也在下午到晚上之间随机
        fake_date = fake_date.replace(
            hour=random.randint(15, 21),
            minute=random.randint(0, 59),
            second=random.randint(0, 59),
        )

        success = upload_run(
            stuno, school, uid, step_dist, end_ts=int(fake_date.timestamp())
        )

        if success:
            # 给服务端一点落库时间，再回查里程
            time.sleep(3)
            new_km = get_real_distance(stuno, uid, school)

            if new_km > current_km:
                print(f"  -> 数据已被服务器采纳！进度: {current_km}km -> {new_km}km / {target_km}km")
                current_km = new_km
                fail_count = 0
            else:
                print("  -> 上传返回成功，但总里程没变。可能该日期已有记录。")
                fail_count += 1
        else:
            fail_count += 1

        # 连续失败就停手，避免把账号刷成异常
        if fail_count >= 5:
            print("[!] 连续 5 次未成功，为账号安全自动中止。")
            break

        fake_date += datetime.timedelta(days=1)

        if fake_date > datetime.datetime.now():
            print("[!] 已经补到今天了，强制停止，避免覆盖真实数据。")
            break

    if current_km >= target_km:
        print(f"\n=== 目标达成！当前总里程: {current_km} km ===")
    else:
        print(f"\n=== 未达标，当前总里程: {current_km} km ===")


# ============================================================
# ⑥ 入口
# ============================================================

def main():
    print("=== 校园跑数据自动上传（自用版）===")
    print("仅限个人学习研究，严禁商业用途，使用后果自负。\n")

    stuno = STUDENT_NO.strip() or input("请输入学号: ").strip()
    if not stuno:
        print("[!] 学号不能为空。")
        return

    school = str(SCHOOL_NO)
    password = hashlib.md5(stuno.encode()).hexdigest()
    uid = login(stuno, password, school)
    if not uid:
        return

    current_km = get_real_distance(stuno, uid, school)
    if current_km < 0:
        print("[!] 无法获取当前里程，按 0 处理。")
        current_km = 0.0
    else:
        print(f"[+] 系统查询：当前已跑总里程为 {current_km} km")

    mode = str(AUTO_MODE)
    if mode not in ("1", "2"):
        print("\n请选择打卡模式:")
        print("  1. 佛系模式：跑当天的量（默认约 4km）")
        print("  2. 暴走模式：穿越时间，一天一天往前补，直到总里程达标")
        mode = input("请输入模式编号 (1 或 2): ").strip()

    if mode == "1":
        run_once(stuno, school, uid)
    elif mode == "2":
        target_km = float(TARGET_TOTAL_KM)
        if current_km >= target_km:
            print("[*] 当前里程已达标，不需要再刷。")
            return
        run_time_travel(stuno, school, uid, current_km, target_km)
    else:
        print("[!] 无效的模式编号。")


# 兼容旧版 run.py / fast_run 的 import 写法
loggin = login

if __name__ == "__main__":
    main()
