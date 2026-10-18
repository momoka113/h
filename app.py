from flask import Flask, jsonify, request, render_template, session, redirect, url_for, flash
from urllib.parse import urlparse, urljoin
from functools import wraps
import json
import os
import re
import tempfile
import fcntl
import threading
import urllib.request
from datetime import datetime, timedelta, timezone

# app.py はプロジェクト直下に置く。
# 実体（templates / static / data）は bousai_app/ 配下にあるので、そこを参照する。
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.join(BASE_DIR, 'bousai_app')

app = Flask(
    __name__,
    template_folder=os.path.join(APP_DIR, 'templates'),
    static_folder=os.path.join(APP_DIR, 'static'),
)
app.secret_key = 'your-secret-key-here'

# 管理者認証情報
ADMIN_CREDENTIALS = {
    'admin': '123'
}

# ────────────────────────────────
# 気象警報・注意報設定
PREFECTURE_CODE = "020000"  # 青森県
AREA_NAME = "青森市"

# 青森市の市区町村コード
AREA_CODE = "0220100"

WARNING_URL = (
    f"https://www.jma.go.jp/bosai/warning/data/r8/{PREFECTURE_CODE}.json"
)

JST = timezone(timedelta(hours=9))

# 警報・注意報のコード一覧
WARNING_CODES = {
    "00": "解除",
    "02": "暴風雪警報",
    "03": "レベル3大雨警報",
    "04": "洪水警報",
    "05": "暴風警報",
    "06": "大雪警報",
    "07": "波浪警報",
    "08": "レベル3高潮警報",
    "09": "レベル3土砂災害警報",
    "10": "レベル2大雨注意報",
    "12": "大雪注意報",
    "13": "風雪注意報",
    "14": "雷注意報",
    "15": "強風注意報",
    "16": "波浪注意報",
    "17": "融雪注意報",
    "18": "洪水注意報",
    "19": "レベル2高潮注意報",
    "20": "濃霧注意報",
    "21": "乾燥注意報",
    "22": "なだれ注意報",
    "23": "低温注意報",
    "24": "霜注意報",
    "25": "着氷注意報",
    "26": "着雪注意報",
    "27": "その他の注意報",
    "29": "レベル2土砂災害注意報",
    "32": "暴風雪特別警報",
    "33": "レベル5大雨特別警報",
    "35": "暴風特別警報",
    "36": "大雪特別警報",
    "37": "波浪特別警報",
    "38": "レベル5高潮特別警報",
    "39": "レベル5土砂災害特別警報",
    "43": "レベル4大雨危険警報",
    "48": "レベル4高潮危険警報",
    "49": "レベル4土砂災害危険警報"
}

# ────────────────────────────────
# サンプルデータの読み込み
DATA_FILE = os.path.join(APP_DIR, 'data', 'shelters.json')
INSTRUCTIONS_FILE = os.path.join(APP_DIR, 'data', 'instructions.json')

def load_json(path, default):
    """JSONファイルを読み込む（存在しない・壊れている場合は default を返す）"""
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default

shelters = load_json(DATA_FILE, [])
instructions = load_json(INSTRUCTIONS_FILE, [])
instruction_update_lock = threading.RLock()
INSTRUCTION_TARGETS = ('住民', '職員', '各部署')
INSTRUCTION_DEPARTMENTS = (
    '道路管理部署', '福祉部署', '避難所部署', '広報部署'
)
INSTRUCTION_DISTRICTS = ('A地区', 'B地区')
INSTRUCTION_URGENCIES = ('高', '中', '低')
INSTRUCTION_STATUSES = ('未対応', '対応中', '完了')

def get_current_shelters():
    """最新の避難所データをファイルから読み込む"""
    current_shelters = load_json(DATA_FILE, None)
    return current_shelters if isinstance(current_shelters, list) else shelters


def load_shelters_for_update():
    """登録前に最新の避難所データを検証して読み込む"""
    try:
        with open(DATA_FILE, encoding='utf-8') as f:
            current_shelters = json.load(f)
    except FileNotFoundError:
        return []

    if not isinstance(current_shelters, list):
        raise ValueError('避難所データの形式が正しくありません。')
    return current_shelters


def get_latest_notice_update(notices):
    """発信データに記録された最新更新日時を返す"""
    dated_notices = [
        notice for notice in notices
        if notice.get('updated_at') or notice.get('created_at')
    ]
    if not dated_notices:
        return None

    def notice_datetime(notice):
        value = notice.get('updated_at') or notice.get('created_at')
        try:
            return datetime.strptime(value, "%Y年%m月%d日 %H:%M")
        except (TypeError, ValueError):
            return datetime.min

    latest_notice = max(dated_notices, key=notice_datetime)
    return latest_notice.get('updated_at') or latest_notice.get('created_at')


def is_high_priority_notice(notice):
    """避難行動を急ぐ必要がある住民向け発信か判定する"""
    urgency = str(notice.get('urgency', '')).strip()
    content = str(notice.get('content', '')).strip()
    return (
        urgency in ('高', '緊急', 'high', 'urgent')
        or '避難指示' in content
        or '避難してください' in content
        or '避難勧告' in content
        or '緊急' in content
    )


def load_instructions_for_update():
    """指示を更新する前に最新の保存データを検証して読み込む"""
    try:
        with open(INSTRUCTIONS_FILE, encoding='utf-8') as f:
            current_instructions = json.load(f)
    except FileNotFoundError:
        return []

    if not isinstance(current_instructions, list):
        raise ValueError('指示データの形式が正しくありません。')
    return current_instructions


def save_instructions(instruction_records):
    """指示を一時ファイル経由で置換し、途中書き込みによる破損を防ぐ"""
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode='w',
            encoding='utf-8',
            dir=os.path.dirname(INSTRUCTIONS_FILE),
            delete=False
        ) as f:
            temp_path = f.name
            json.dump(instruction_records, f, ensure_ascii=False, indent=2)
        os.replace(temp_path, INSTRUCTIONS_FILE)
    except Exception:
        if temp_path and os.path.exists(temp_path):
            os.unlink(temp_path)
        raise


def instruction_timestamp(value):
    """保存日時を並べ替え・表示用のJST日時に変換する"""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.strptime(value, "%Y年%m月%d日 %H:%M").replace(tzinfo=JST)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=JST)
    return parsed.astimezone(JST)


def format_instruction_timestamp(value):
    parsed = instruction_timestamp(value)
    return parsed.strftime("%Y年%m月%d日 %H:%M") if parsed else '不明'


def instruction_sort_key(instruction):
    created_at = instruction.get('created_at')
    parsed = instruction_timestamp(created_at)
    return (parsed is not None, parsed or datetime.min.replace(tzinfo=JST))


def instruction_recipient_label(instruction):
    target = instruction.get('target')
    if target == '住民':
        return '住民向け'
    if target == '職員':
        return '職員向け'
    if target == '各部署':
        department = instruction.get('department')
        return f'各部署向け（{department}）' if department else '各部署向け'
    if isinstance(target, str) and target.strip():
        return f'職員向け（{target.strip()}）'
    return '宛先不明'


def instruction_status_label(instruction):
    status = instruction.get('status')
    if status in INSTRUCTION_STATUSES:
        return status
    if status == '解除':
        return '完了'
    return '未対応'


def instruction_shelter_name(instruction, shelters_by_id):
    shelter_id = instruction.get('shelter_id')
    if shelter_id is not None:
        shelter = shelters_by_id.get(str(shelter_id))
        if shelter:
            name = shelter.get('name')
            return name.strip() if isinstance(name, str) and name.strip() else '避難先不明'
    legacy_name = instruction.get('shelter')
    return legacy_name.strip() if isinstance(legacy_name, str) and legacy_name.strip() else '指定なし'


def save_new_instruction(values):
    """プロセス間ロック下でIDを採番し、最新データへ指示を追加する"""
    global instructions
    lock_path = INSTRUCTIONS_FILE + '.lock'
    with instruction_update_lock:
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            current_instructions = load_instructions_for_update()
            existing_ids = [
                item.get('id') for item in current_instructions
                if isinstance(item, dict)
                and isinstance(item.get('id'), int)
                and not isinstance(item.get('id'), bool)
            ]
            used_ids = {
                str(item['id']) for item in current_instructions
                if isinstance(item, dict)
                and isinstance(item.get('id'), (int, str))
                and not isinstance(item.get('id'), bool)
            }
            new_id = max(existing_ids, default=0) + 1
            while str(new_id) in used_ids:
                new_id += 1
            now = get_japan_time()
            instruction = {
                'id': new_id,
                'target': values['target'],
                'department': values['department'] if values['target'] == '各部署' else '',
                'content': values['content'],
                'urgency': values['urgency'],
                'district': values['district'],
                'shelter_id': values['shelter_id'],
                'shelter': values['shelter_name'],
                'status': '未対応',
                'created_at': now,
                'updated_at': now
            }
            save_instructions(current_instructions + [instruction])
            instructions = current_instructions + [instruction]
            return instruction
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)


def update_instruction_status(instruction_id, status):
    """プロセス間ロック下で指示の対応状況を検証・更新する"""
    global instructions
    lock_path = INSTRUCTIONS_FILE + '.lock'
    with instruction_update_lock:
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            current_instructions = load_instructions_for_update()
            instruction = next(
                (
                    item for item in current_instructions
                    if isinstance(item, dict)
                    and not isinstance(item.get('id'), bool)
                    and item.get('id') == instruction_id
                ),
                None
            )
            if instruction is None:
                return False

            instruction['status'] = status
            instruction['updated_at'] = get_japan_time()
            save_instructions(current_instructions)
            instructions = current_instructions
            return True
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)


def save_shelters(shelter_records):
    """避難所データをファイルに安全に保存する"""
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode='w',
            encoding='utf-8',
            dir=os.path.dirname(DATA_FILE),
            delete=False
        ) as f:
            temp_path = f.name
            json.dump(shelter_records, f, ensure_ascii=False, indent=2)
        os.replace(temp_path, DATA_FILE)
    except Exception:
        if temp_path and os.path.exists(temp_path):
            os.unlink(temp_path)
        raise
# ────────────────────────────────

# ────────────────────────────────
# 認証関連の設定とヘルパー関数
def is_safe_url(target):
    """リダイレクト先URLが安全かどうかチェック"""
    ref_url = urlparse(request.host_url)
    test_url = urlparse(urljoin(request.host_url, target))
    return test_url.scheme in ('http', 'https') and ref_url.netloc == test_url.netloc

def login_required(f):
    """認証が必要なページに付けるデコレータ"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('logged_in'):
            # 現在のURLをnextパラメータとしてログイン画面にリダイレクト
            return redirect(url_for('login', next=request.url))
        return f(*args, **kwargs)
    return decorated_function

def get_japan_time():
    """日本時間（JST）の現在時刻を取得する"""
    return datetime.now(JST).strftime("%Y年%m月%d日 %H:%M")


def format_report_time(iso_str):
    """気象庁の発表時刻（ISO形式）をJSTの表示用文字列に変換する"""
    if not iso_str:
        return "不明"
    try:
        parsed = datetime.fromisoformat(iso_str.replace('Z', '+00:00'))
        if parsed.tzinfo:
            parsed = parsed.astimezone(JST)
        return parsed.strftime("%Y年%m月%d日 %H:%M")
    except ValueError:
        return iso_str


def filter_shelters(district=None):
    """district 指定があれば一致する避難所のみ、なければ全件を返す"""
    current_shelters = get_current_shelters()
    return [
        s for s in current_shelters
        if not district or get_shelter_district(s) == district
    ]


def get_shelter_district(shelter):
    """地区項目がない古いデータでは住所から地区名を補完する"""
    district = shelter.get('district')
    if isinstance(district, str) and district.strip():
        return district.strip()

    address = shelter.get('address')
    if not isinstance(address, str):
        return ''
    match = re.search(r'市(.+?)(?=\d|[-－])', address)
    return match.group(1) if match else ''


def get_shelter_districts():
    """現在の避難所データから地区選択肢を作成する"""
    return sorted({
        district for shelter in get_current_shelters()
        if (district := get_shelter_district(shelter))
    })


REGISTER_HAZARDS = (
    '地震', '津波', '洪水', '土砂災害', '高潮', '大規模火災', '火山噴火'
)
SEARCH_HAZARDS = REGISTER_HAZARDS
REGISTER_FACILITIES = (
    'ペット可', 'バリアフリー', '備蓄あり', 'Wi-Fi', '授乳スペース'
)
OPENING_STATUSES = ('開設中', '閉鎖中')
SEARCH_PET_VALUES = ('allowed', 'not_allowed')


def validate_search_params(args):
    """避難所検索のGETパラメータを検証して正規化する"""
    keyword = args.get('keyword', '').strip()
    district = args.get('district', '').strip()
    hazards = args.getlist('hazard')
    facilities = args.getlist('facility')
    pets = args.getlist('pet')
    barrier_free = args.get('barrier_free', '')

    invalid_hazards = [value for value in hazards if value not in SEARCH_HAZARDS]
    invalid_facilities = [
        value for value in facilities if value not in REGISTER_FACILITIES
    ]
    invalid_pets = [value for value in pets if value not in SEARCH_PET_VALUES]
    if (
        invalid_hazards
        or invalid_facilities
        or invalid_pets
        or barrier_free not in ('', 'true')
        or (district and district not in get_shelter_districts())
    ):
        raise ValueError('検索条件に不正な値が含まれています。')

    return {
        'keyword': keyword,
        'district': district,
        'hazards': list(dict.fromkeys(hazards)),
        'facilities': list(dict.fromkeys(facilities)),
        'pets': list(dict.fromkeys(pets)),
        'barrier_free': barrier_free == 'true'
    }


def search_shelters(criteria):
    """避難所検索条件をすべて満たす施設を返す"""
    results = []
    keyword = criteria['keyword'].casefold()
    for shelter in get_current_shelters():
        name = str(shelter.get('name', ''))
        if keyword and keyword not in name.casefold():
            continue
        if criteria['district'] and get_shelter_district(shelter) != criteria['district']:
            continue

        if criteria['hazards']:
            shelter_hazards = shelter.get('hazards', [])
            if not isinstance(shelter_hazards, list):
                shelter_hazards = []
            if not any(hazard in shelter_hazards for hazard in criteria['hazards']):
                continue

        if criteria['facilities']:
            shelter_facilities = shelter.get('facilities', [])
            shelter_facilities = (
                list(shelter_facilities) if isinstance(shelter_facilities, list) else []
            )
            if shelter.get('pet_allowed', shelter.get('pets_allowed')) is True:
                shelter_facilities.append('ペット可')
            if shelter.get('barrier_free') is True:
                shelter_facilities.append('バリアフリー')
            if not any(facility in shelter_facilities for facility in criteria['facilities']):
                continue

        pet_allowed = shelter.get('pet_allowed', shelter.get('pets_allowed'))
        if criteria['pets'] and len(criteria['pets']) == 1:
            if criteria['pets'][0] == 'allowed' and pet_allowed is not True:
                continue
            if criteria['pets'][0] == 'not_allowed' and pet_allowed is not False:
                continue

        if criteria['barrier_free'] and shelter.get('barrier_free') is not True:
            continue
        results.append(shelter)
    return results


def parse_area_warnings(warning_data):
    """気象庁の新形式JSONから対象市区町村の発表・継続中の情報を抽出する"""
    if not isinstance(warning_data, list):
        raise ValueError("気象庁の警報・注意報データが新形式の配列ではありません")

    warnings = []
    seen_codes = set()
    report_datetimes = []

    for report in warning_data:
        if not isinstance(report, dict):
            continue

        report_datetime = report.get("reportDatetime")
        if isinstance(report_datetime, str) and report_datetime:
            report_datetimes.append(report_datetime)

        warning = report.get("warning")
        if not isinstance(warning, dict):
            continue

        class20_items = warning.get("class20Items", [])
        if not isinstance(class20_items, list):
            continue

        area = next(
            (
                item for item in class20_items
                if isinstance(item, dict)
                and item.get("areaCode") == AREA_CODE
            ),
            None
        )
        if not area:
            continue

        kinds = area.get("kinds", [])
        if not isinstance(kinds, list):
            continue

        for kind in kinds:
            if not isinstance(kind, dict):
                continue

            status = kind.get("status", "")
            code = kind.get("code", "")
            if status not in ("発表", "継続") or not code or code in seen_codes:
                continue

            warnings.append({
                "name": WARNING_CODES.get(
                    code,
                    f"不明な警報・注意報 (コード: {code})"
                ),
                "code": code,
                "status": status
            })
            seen_codes.add(code)

    latest_report_datetime = max(report_datetimes, default="")
    return warnings, latest_report_datetime


def get_weather_warnings():
    """対象市区町村の警報・注意報を取得する"""
    try:
        # 青森県の新形式（令和8年～）警報・注意報データを取得
        with urllib.request.urlopen(url=WARNING_URL, timeout=10) as res:
            warning_data = json.loads(res.read())

        warnings, report_datetime = parse_area_warnings(warning_data)

        return {
            "area_name": AREA_NAME,
            "warnings": warnings,
            "report_time": format_report_time(report_datetime),
            "last_fetch_time": get_japan_time()
        }

    except Exception:
        return {
            "area_name": AREA_NAME,
            "warnings": [],
            "report_time": "取得失敗",
            "last_fetch_time": get_japan_time(),
            "error": True
        }


# トップページ：templates/index.html を返す（住民向け指示も表示する）
@app.route('/')
def index():
    global instructions
    try:
        current_instructions = load_instructions_for_update()
    except (OSError, ValueError, json.JSONDecodeError):
        app.logger.exception('発信データの読み込みに失敗しました')
        return '発信情報を読み込めませんでした。時間をおいて再度お試しください。', 500
    instructions = current_instructions
    resident_notices = [
        item for item in current_instructions
        if isinstance(item, dict) and item.get('target') == '住民'
    ]
    emergency_notices = [
        notice for notice in resident_notices
        if notice.get('status') not in ('解除', '完了')
    ]
    new_directives = sorted(
        emergency_notices,
        key=lambda notice: (
            not is_high_priority_notice(notice),
            str(notice.get('updated_at') or notice.get('created_at') or '')
        )
    )
    return render_template(
        'index.html',
        resident_notices=resident_notices,
        emergency_notices=emergency_notices,
        new_directives=new_directives,
        is_high_priority_notice=is_high_priority_notice,
        latest_notice_update=get_latest_notice_update(resident_notices)
    )

# ログインページ
@app.route('/login', methods=['GET', 'POST'])
def login():
    # リダイレクト先を取得（デフォルトは避難所登録画面）
    next_url = request.args.get('next') or request.form.get('next')

    # 安全でないURLの場合はデフォルトページにリダイレクト
    if not next_url or not is_safe_url(next_url):
        next_url = url_for('shelter_register')

    if request.method == 'POST':
        password = request.form.get('password', '').strip()

        # 認証チェック
        username = next(
            (name for name, registered_password in ADMIN_CREDENTIALS.items()
             if registered_password == password),
            None
        )
        if username:
            session['logged_in'] = True
            session['username'] = username
            # ログイン成功後は指定されたページにリダイレクト
            return redirect(next_url)
        return render_template('login.html', error=True, message="パスワードが正しくありません。", next=next_url)

    # ログイン済みの場合は指定されたページにリダイレクト
    if session.get('logged_in'):
        return redirect(next_url)

    return render_template('login.html', next=next_url)

# ログアウト
@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))

# 避難所情報登録ページ
@app.route('/shelter_register', methods=['GET', 'POST'])
@login_required
def shelter_register():
    values = {
        'name': '',
        'address': '',
        'capacity': '',
        'opening_status': '閉鎖中',
        'hazards': [],
        'facilities': []
    }

    if request.method == 'POST':
        values = {
            'name': request.form.get('name', ''),
            'address': request.form.get('address', ''),
            'capacity': request.form.get('capacity', ''),
            'opening_status': request.form.get('opening_status', ''),
            'hazards': request.form.getlist('hazards'),
            'facilities': request.form.getlist('facilities')
        }
        name = values['name'].strip()
        if not name:
            name_error = '避難所名を入力してください。'
        elif len(name) > 200:
            name_error = '避難所名は200文字以内で入力してください。'
        else:
            name_error = None

        address = values['address'].strip()
        if not address:
            address_error = '住所を入力してください。'
        elif len(address) > 300:
            address_error = '住所は300文字以内で入力してください。'
        else:
            address_error = None

        capacity_text = values['capacity'].strip()
        capacity = None
        if not capacity_text:
            capacity_error = '収容人数を入力してください。'
        elif not re.fullmatch(r'[0-9]+', capacity_text):
            capacity_error = '収容人数は整数で入力してください。'
        else:
            try:
                capacity = int(capacity_text)
            except ValueError:
                capacity_error = '収容人数は整数で入力してください。'
            else:
                capacity_error = (
                    '収容人数は1人以上で入力してください。'
                    if capacity < 1 else None
                )

        invalid_hazards = [
            value for value in values['hazards']
            if value not in REGISTER_HAZARDS
        ]
        invalid_facilities = [
            value for value in values['facilities']
            if value not in REGISTER_FACILITIES
        ]
        hazard_error = (
            '災害種別に不正な選択肢が含まれています。'
            if invalid_hazards else None
        )
        facility_error = (
            '設備・対応に不正な選択肢が含まれています。'
            if invalid_facilities else None
        )
        opening_status_error = (
            '開設状況を選択してください。'
            if values['opening_status'] not in OPENING_STATUSES else None
        )

        errors = [
            error for error in (
                name_error, address_error, capacity_error,
                hazard_error, facility_error, opening_status_error
            )
            if error
        ]
        if errors:
            return render_template(
                'shelter_register.html',
                error=True,
                errors=errors,
                values=values,
                hazards=REGISTER_HAZARDS,
                facilities=REGISTER_FACILITIES,
                shelters=get_current_shelters()
            ), 400

        values['name'] = name
        values['address'] = address
        values['capacity'] = capacity_text
        selected_hazards = list(dict.fromkeys(values['hazards']))
        selected_facilities = list(dict.fromkeys(values['facilities']))
        action = request.form.get('action', 'register')
        if action == 'confirm':
            return render_template(
                'shelter_register_confirm.html',
                values=values,
                hazards=selected_hazards,
                facilities=selected_facilities
            )
        if action == 'back':
            return render_template(
                'shelter_register.html',
                values=values,
                hazards=REGISTER_HAZARDS,
                facilities=REGISTER_FACILITIES,
                shelters=get_current_shelters()
            )
        if action != 'register':
            return render_template(
                'shelter_register.html',
                error=True,
                errors=['操作を確認できませんでした。入力内容を確認してください。'],
                values=values,
                hazards=REGISTER_HAZARDS,
                facilities=REGISTER_FACILITIES,
                shelters=get_current_shelters()
            ), 400

        try:
            current_shelters = load_shelters_for_update()
            existing_ids = [
                shelter['id']
                for shelter in current_shelters
                if isinstance(shelter, dict)
                and isinstance(shelter.get('id'), int)
                and not isinstance(shelter.get('id'), bool)
            ]
            new_shelter = {
                'id': max(existing_ids, default=0) + 1,
                'name': name,
                'address': address,
                'opening_status': values['opening_status'],
                'capacity': capacity,
                'evacuees': 0,
                'hazards': selected_hazards,
                'facilities': selected_facilities,
                'pet_allowed': 'ペット可' in selected_facilities,
                'barrier_free': 'バリアフリー' in selected_facilities
            }
            updated_shelters = current_shelters + [new_shelter]
            save_shelters(updated_shelters)
        except (OSError, ValueError, json.JSONDecodeError):
            app.logger.exception('避難所データの保存に失敗しました')
            return render_template(
                'shelter_register.html',
                error=True,
                errors=['避難所を保存できませんでした。時間をおいて再度お試しください。'],
                values=values,
                hazards=REGISTER_HAZARDS,
                facilities=REGISTER_FACILITIES,
                shelters=get_current_shelters()
            ), 500

        shelters[:] = updated_shelters
        values = {
            'name': '',
            'address': '',
            'capacity': '',
            'opening_status': '閉鎖中',
            'hazards': [],
            'facilities': []
        }
        return render_template(
            'shelter_register.html',
            success=True,
            values=values,
            hazards=REGISTER_HAZARDS,
            facilities=REGISTER_FACILITIES,
            shelters=updated_shelters
        )

    return render_template(
        'shelter_register.html',
        values=values,
        hazards=REGISTER_HAZARDS,
        facilities=REGISTER_FACILITIES,
        shelters=get_current_shelters()
    )

# 避難所の開設状況編集ページ
@app.route('/shelters/<int:shelter_id>/edit', methods=['GET', 'POST'])
@login_required
def shelter_edit(shelter_id):
    try:
        current_shelters = load_shelters_for_update()
    except (OSError, ValueError, json.JSONDecodeError):
        app.logger.exception('避難所データの読み込みに失敗しました')
        return render_template(
            'shelter_edit.html',
            error=True,
            message='避難所データを読み込めませんでした。時間をおいて再度お試しください。',
            statuses=OPENING_STATUSES,
            shelter=None,
            opening_status=''
        ), 500

    shelter = next(
        (
            record for record in current_shelters
            if isinstance(record, dict) and record.get('id') == shelter_id
        ),
        None
    )
    if shelter is None:
        return render_template(
            'shelter_edit.html',
            shelter=None,
            statuses=OPENING_STATUSES,
            opening_status=''
        ), 404

    opening_status = request.form.get(
        'opening_status',
        shelter.get('opening_status')
        if shelter.get('opening_status') in OPENING_STATUSES else ''
    )
    if request.method == 'POST':
        if opening_status not in OPENING_STATUSES:
            return render_template(
                'shelter_edit.html',
                shelter=shelter,
                statuses=OPENING_STATUSES,
                opening_status=opening_status,
                error=True,
                message='開設状況は「開設中」または「閉鎖中」を選択してください。'
            ), 400

        shelter['opening_status'] = opening_status
        try:
            save_shelters(current_shelters)
        except (OSError, ValueError, json.JSONDecodeError):
            app.logger.exception('避難所の開設状況の保存に失敗しました')
            return render_template(
                'shelter_edit.html',
                shelter=shelter,
                statuses=OPENING_STATUSES,
                opening_status=opening_status,
                error=True,
                message='開設状況を保存できませんでした。時間をおいて再度お試しください。'
            ), 500

        shelters[:] = current_shelters
        return render_template(
            'shelter_edit.html',
            shelter=shelter,
            statuses=OPENING_STATUSES,
            opening_status=opening_status,
            success=True
        )

    return render_template(
        'shelter_edit.html',
        shelter=shelter,
        statuses=OPENING_STATUSES,
        opening_status=opening_status
    )

# 避難所検索ページ
@app.route('/shelter_search')
def shelter_search():
    return render_template(
        'shelter_search.html',
        districts=get_shelter_districts(),
        hazards=SEARCH_HAZARDS,
        facilities=REGISTER_FACILITIES
    )

# 全施設一覧ページ
@app.route('/all_shelters')
def all_shelters():
    return render_template(
        'search_results.html',
        results=get_current_shelters(),
        search_criteria=None,
        is_all_shelters=True,
        districts=get_shelter_districts()
    )


# 指示ボード：住民向け一覧は公開し、登録・内部情報はログイン利用者に限定する
@app.route('/board', methods=['GET', 'POST'])
def board():
    if request.method == 'POST' and not session.get('logged_in'):
        return redirect(url_for('login', next=request.url))

    values = {
        'target': '住民',
        'department': '',
        'urgency': '中',
        'district': '',
        'shelter_id': '',
        'content': ''
    }
    errors = []

    try:
        available_shelters = load_shelters_for_update()
    except (OSError, ValueError, json.JSONDecodeError):
        app.logger.exception('避難先データの読み込みに失敗しました')
        return render_template(
            'board.html',
            values=values,
            errors=['避難先データを読み込めませんでした。時間をおいて再度お試しください。'],
            instructions=[],
            shelters=[],
            districts=list(INSTRUCTION_DISTRICTS)
        ), 500

    available_shelters = [
        shelter for shelter in available_shelters
        if (
            isinstance(shelter, dict)
            and isinstance(shelter.get('id'), (int, str))
            and not isinstance(shelter.get('id'), bool)
            and str(shelter.get('id')).strip()
        )
    ]
    districts = sorted({
        district for shelter in available_shelters
        if (district := get_shelter_district(shelter))
    }) or list(INSTRUCTION_DISTRICTS)

    def render_instruction_board(form_values, form_errors=None, status=200):
        try:
            saved_instructions = load_instructions_for_update()
        except (OSError, ValueError, json.JSONDecodeError):
            app.logger.exception('指示データの読み込みに失敗しました')
            return render_template(
                'board.html',
                values=form_values,
                errors=(form_errors or []) + [
                    '指示データを読み込めませんでした。時間をおいて再度お試しください。'
                ],
                instructions=[],
                staff_instructions=[],
                shelters=available_shelters,
                districts=districts
            ), 500

        shelters_by_id = {
            str(shelter['id']): shelter for shelter in available_shelters
        }
        sort_by_urgency = request.args.get('sort') == 'urgency'

        def prepare_display_instructions(targets):
            matching_instructions = [
                item for item in saved_instructions
                if isinstance(item, dict) and item.get('target') in targets
            ]
            if sort_by_urgency:
                urgency_rank = {'高': 3, '中': 2, '低': 1}
                matching_instructions.sort(
                    key=lambda item: (
                        urgency_rank.get(
                            item.get('urgency')
                            if isinstance(item.get('urgency'), str) else '',
                            2
                        ),
                        *instruction_sort_key(item)
                    ),
                    reverse=True
                )
            else:
                matching_instructions.sort(
                    key=instruction_sort_key,
                    reverse=True
                )

            display_instructions = []
            for instruction in matching_instructions:
                display_instruction = dict(instruction)
                if display_instruction.get('urgency') not in INSTRUCTION_URGENCIES:
                    display_instruction['urgency'] = '中'
                display_instruction['recipient_label'] = instruction_recipient_label(instruction)
                display_instruction['display_shelter'] = instruction_shelter_name(
                    instruction, shelters_by_id
                )
                display_instruction['display_created_at'] = format_instruction_timestamp(
                    instruction.get('created_at')
                )
                display_instruction['display_status'] = instruction_status_label(instruction)
                display_instructions.append(display_instruction)
            return display_instructions

        display_instructions = prepare_display_instructions({'住民'})
        staff_instructions = (
            prepare_display_instructions({'職員', '各部署'})
            if session.get('logged_in') else []
        )

        return render_template(
            'board.html',
            values=form_values,
            errors=form_errors or [],
            instructions=display_instructions,
            staff_instructions=staff_instructions,
            shelters=available_shelters,
            districts=districts,
            sort_by_urgency=sort_by_urgency
        ), status

    if request.method == 'POST':
        values = {
            'target': request.form.get('target', '').strip(),
            'department': request.form.get('department', '').strip(),
            'urgency': request.form.get('urgency', '').strip(),
            'district': request.form.get('district', '').strip(),
            'shelter_id': request.form.get('shelter_id', '').strip(),
            'content': request.form.get('content', '').strip()
        }
        if values['target'] not in INSTRUCTION_TARGETS:
            errors.append('指示の宛先を選択してください。')
        if values['urgency'] not in INSTRUCTION_URGENCIES:
            errors.append('緊急度を選択してください。')
        if values['target'] == '各部署':
            if values['department'] not in INSTRUCTION_DEPARTMENTS:
                errors.append('部署を選択してください。')
        elif values['department']:
            errors.append('選択した宛先では部署を指定できません。')
        if values['district'] not in districts:
            errors.append('対象地区を選択してください。')
        if not values['content']:
            errors.append('指示内容を入力してください。')

        matching_shelters = []
        if values['shelter_id']:
            matching_shelters = [
                shelter for shelter in available_shelters
                if str(shelter.get('id')) == values['shelter_id']
            ]
            if len(matching_shelters) != 1:
                errors.append('登録済みの避難先を選択してください。')

        if errors:
            return render_instruction_board(values, errors, status=400)

        selected_shelter = matching_shelters[0] if matching_shelters else None
        shelter_name = selected_shelter.get('name') if selected_shelter else ''
        if selected_shelter and (
            not isinstance(shelter_name, str) or not shelter_name.strip()
        ):
            return render_instruction_board(
                values,
                ['選択した避難先の情報を確認できません。'],
                status=400
            )

        try:
            save_new_instruction({
                **values,
                'shelter_id': selected_shelter['id'] if selected_shelter else '',
                'shelter_name': shelter_name.strip() if shelter_name else ''
            })
        except (OSError, ValueError, json.JSONDecodeError):
            app.logger.exception('指示データの保存に失敗しました')
            return render_instruction_board(
                values,
                ['指示を保存できませんでした。時間をおいて再度お試しください。'],
                status=500
            )
        flash('指示を登録しました。', 'success')
        return redirect(url_for('board'))

    return render_instruction_board(values)


@app.route('/board/<int:instruction_id>/status', methods=['POST'])
@login_required
def update_board_instruction_status(instruction_id):
    status = request.form.get('status', '').strip()
    if status not in INSTRUCTION_STATUSES:
        flash('対応状況は「未対応」「対応中」「完了」から選択してください。', 'error')
        return redirect(url_for('board'))

    try:
        updated = update_instruction_status(instruction_id, status)
    except (OSError, ValueError, json.JSONDecodeError):
        app.logger.exception('指示の対応状況の保存に失敗しました')
        flash('対応状況を保存できませんでした。時間をおいて再度お試しください。', 'error')
        return redirect(url_for('board'))

    if not updated:
        flash('更新する指示が見つかりませんでした。', 'error')
        return redirect(url_for('board'))

    flash('対応状況を更新しました。', 'success')
    return redirect(url_for('board'))


# 検索結果ページ：templates/search_results.html を返す
@app.route('/search_results')
def search_results():
    try:
        criteria = validate_search_params(request.args)
    except ValueError as error:
        return render_template(
            'search_results.html',
            results=[],
            search_criteria=None,
            search_error=str(error),
            is_all_shelters=False,
            districts=get_shelter_districts()
        ), 400

    return render_template(
        'search_results.html',
        results=search_shelters(criteria),
        search_criteria=criteria,
        is_all_shelters=False,
        districts=get_shelter_districts()
    )

# JSON API：/shelters?district=地区名
@app.route('/shelters', methods=['GET'])
def get_shelters():
    results = filter_shelters(request.args.get('district'))

    if not results:
        # 見つからなければエラー JSON を返す
        return jsonify({'error': 'No shelters found'}), 404

    # 見つかったらリストを JSON で返す
    return jsonify(results)

# 気象警報・注意報API
@app.route('/api/weather_warnings')
def api_weather_warnings():
    """気象警報・注意報をJSON形式で返すAPI"""
    return jsonify(get_weather_warnings())

if __name__ == '__main__':
    app.run(debug=True, port=5000)
