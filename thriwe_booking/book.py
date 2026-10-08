"""Thriwe (Emirates NBD) 골프 자동 예약 스크립트.

아부다비(Asia/Dubai) 자정에 열리는 2주 뒤 골프 예약을 자동으로 진행한다.
사이트의 실제 화면 요소(selector)는 config.toml 의 steps 로 정의한다.

사용법:
    python book.py --config config.toml --dry-run   # 결제 직전까지만 진행
    python book.py --config config.toml             # 실제 예약
    python book.py --config config.toml --now       # 자정 대기 없이 즉시 실행(테스트)
"""
import argparse
import datetime as dt
import email.utils
import os
import re
import sys
import time
import tomllib
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

BASE_DIR = Path(__file__).resolve().parent

# 로그에 값을 그대로 찍으면 안 되는 변수
SENSITIVE_VARS = {'password', 'card_number', 'card_expiry', 'card_cvv', 'card_name',
                  'card_middle', 'card_exp_month', 'card_exp_year'}
ENV_VARS = {
    'email': 'THRIWE_EMAIL',
    'password': 'THRIWE_PASSWORD',
    'card_number': 'CARD_NUMBER',
    'card_expiry': 'CARD_EXPIRY',
    'card_cvv': 'CARD_CVV',
    'card_name': 'CARD_NAME',
}


def log(msg):
    print(f'[{dt.datetime.now().strftime("%H:%M:%S.%f")[:-3]}] {msg}', flush=True)


def load_dotenv(path):
    if not path.exists():
        return
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def server_clock_offset(url):
    """사이트 응답의 Date 헤더로 (서버시간 - 로컬시간) 초 단위 오차를 구한다."""
    try:
        req = urllib.request.Request(url, method='HEAD')
        start = time.time()
        with urllib.request.urlopen(req, timeout=10) as res:
            header = res.headers.get('Date')
        end = time.time()
        if not header:
            return 0.0
        server = email.utils.parsedate_to_datetime(header).timestamp()
        # Date 헤더는 초 단위로 버림되므로 0.5초 보정
        return server + 0.5 - (start + end) / 2
    except Exception as e:  # noqa: BLE001 - 오차 측정 실패 시 로컬 시간 사용
        log(f'서버 시간 확인 실패, 로컬 시간을 사용합니다: {e}')
        return 0.0


def next_open_time(schedule_cfg, now=None):
    tz = ZoneInfo(schedule_cfg.get('timezone', 'Asia/Dubai'))
    now = now or dt.datetime.now(tz)
    hh, mm, ss = (int(x) for x in schedule_cfg.get('open_time', '00:00:00').split(':'))
    target = now.replace(hour=hh, minute=mm, second=ss, microsecond=0)
    # 오픈 직후 몇 분 안에 실행했다면 다음 날이 아니라 방금 열린 회차를 노린다
    grace = dt.timedelta(minutes=float(schedule_cfg.get('late_grace_minutes', 10)))
    if target + grace <= now:
        target += dt.timedelta(days=1)
    return target


def build_vars(cfg, open_at):
    sched = cfg['schedule']
    play_date = open_at.date() + dt.timedelta(days=int(sched.get('days_ahead', 14)))
    variables = {key: os.environ.get(env, '') for key, env in ENV_VARS.items()}
    # 결제 화면: 카드번호 앞 6자리/뒤 4자리는 미리 채워져 있어 가운데만 입력, 유효기간은 MM / YY 칸이 따로 있음
    digits = re.sub(r'\D', '', variables['card_number'])
    variables['card_middle'] = digits[6:-4] if len(digits) > 10 else ''
    exp = re.findall(r'\d+', variables['card_expiry'])
    variables['card_exp_month'] = exp[0].zfill(2) if exp else ''
    variables['card_exp_year'] = exp[1][-2:] if len(exp) > 1 else ''
    variables['play_date'] = play_date.strftime(sched.get('date_format', '%Y-%m-%d'))
    variables['play_day'] = str(play_date.day)
    # 달력(react-datepicker)의 날짜 이름 형식: "Thursday, October 22nd, 2026"
    variables['play_date_label'] = (f'{play_date:%A}, {play_date:%B} {ordinal(play_date.day)}, '
                                    f'{play_date.year}')
    variables.update({k: str(v) for k, v in cfg.get('vars', {}).items()})
    return variables


def parse_tee(text):
    """'07:30', '14:10', '8:10AM' -> tee_hour/tee_minute/tee_ampm (12시간제, 두 자리)"""
    m = re.fullmatch(r'\s*(\d{1,2}):(\d{2})\s*([AaPp][Mm])?\s*', text)
    if not m:
        sys.exit(f'--tee 형식이 잘못됐습니다: {text} (예: 07:30, 14:10, 8:10AM)')
    hour, minute, ampm = int(m.group(1)), m.group(2), m.group(3)
    if ampm:
        ampm = ampm.upper()
    else:
        if hour > 23:
            sys.exit(f'--tee 시간이 잘못됐습니다: {text}')
        ampm = 'PM' if hour >= 12 else 'AM'
        hour = hour % 12 or 12
    if not 1 <= hour <= 12:
        sys.exit(f'--tee 시간이 잘못됐습니다: {text}')
    return {'tee_hour': f'{hour:02d}', 'tee_minute': minute, 'tee_ampm': ampm}


def ordinal(n):
    suffix = 'th' if 11 <= n % 100 <= 13 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')
    return f'{n}{suffix}'


def render(value, variables):
    if value is None:
        return None
    return str(value).format(**variables)


def describe(step, variables):
    text = step['action']
    if 'selector' in step:
        text += f' {render(step["selector"], variables)}'
    if 'selectors' in step:
        text += f' {[render(s, variables) for s in step["selectors"]]}'
    if 'value' in step:
        hidden = any('{' + k + '}' in str(step['value']) for k in SENSITIVE_VARS)
        text += ' = ****' if hidden else f' = {render(step["value"], variables)}'
    return text


class Runner:
    def __init__(self, page, cfg, variables, open_at, clock_offset, dry_run, shot_dir):
        self.page = page
        self.cfg = cfg
        self.vars = variables
        self.open_at = open_at
        self.clock_offset = clock_offset
        self.dry_run = dry_run
        self.shot_dir = shot_dir
        self.timeout_ms = int(cfg.get('browser', {}).get('timeout_ms', 15000))

    def screenshot(self, name):
        path = self.shot_dir / f'{dt.datetime.now():%Y%m%d_%H%M%S}_{name}.png'
        try:
            self.page.screenshot(path=str(path), full_page=True)
            log(f'스크린샷 저장: {path}')
        except PlaywrightError as e:
            log(f'스크린샷 실패: {e}')

    def wait_until_open(self, step):
        early = float(step.get('early_seconds', 0))
        target = self.open_at.timestamp() - early
        log(f'예약 오픈 대기: {self.open_at.isoformat()} (서버 오차 {self.clock_offset:+.2f}s)')
        while True:
            remaining = target - (time.time() + self.clock_offset)
            if remaining <= 0:
                break
            if remaining > 30:
                # 대기 중 세션이 끊기지 않도록 주기적으로 페이지를 깨운다
                self.page.wait_for_timeout(min(remaining - 30, 60) * 1000)
            else:
                time.sleep(min(remaining, 0.05))
        log('오픈 시각 도달')

    def wait_next_page(self, step, target):
        """클릭 후 until 요소가 보일 때까지 기다린다.
        same_page 요소가 아직 보이면(클릭이 무시돼 화면이 그대로면) 다시 클릭한다.
        화면이 이미 바뀌는 중이면 다시 누르지 않는다 (Next 는 화면마다 이름이 같아 화면을 건너뛸 수 있음)."""
        # until 은 하나 또는 목록 (목록이면 그중 하나라도 보이면 다음 화면으로 판단)
        targets = step['until'] if isinstance(step['until'], list) else [step['until']]
        until = self.page.locator(render(targets[0], self.vars))
        for other in targets[1:]:
            until = until.or_(self.page.locator(render(other, self.vars)))
        until = until.first
        same_page = render(step.get('same_page'), self.vars)
        deadline = time.time() + int(step.get('until_timeout_ms', 15000)) / 1000
        while True:
            try:
                until.wait_for(timeout=2000)
                return
            except PlaywrightError:
                if time.time() > deadline:
                    raise
                if same_page and self.page.locator(same_page).first.is_visible() and target.is_visible():
                    log('  화면이 넘어가지 않아 다시 클릭')
                    target.click(timeout=self.timeout_ms)

    def do(self, step, timeout_ms=None):
        action = step['action']
        timeout = timeout_ms or self.timeout_ms
        page = self.page
        selector = render(step.get('selector'), self.vars)
        value = render(step.get('value'), self.vars)

        if action == 'goto':
            page.goto(render(step['url'], self.vars), wait_until=step.get('wait_until', 'domcontentloaded'))
        elif action == 'reload':
            page.reload(wait_until=step.get('wait_until', 'domcontentloaded'))
        elif action == 'fill':
            loc = page.locator(selector).first
            loc.fill(value, timeout=timeout)
            if step.get('press'):
                loc.press(step['press'])
            # 페이지가 다시 그려지며 입력값이 지워지는 경우가 있어 확인 후 재시도
            if step.get('verify', True):
                page.wait_for_timeout(int(step.get('verify_after_ms', 300)))
            if step.get('verify', True) and loc.input_value(timeout=timeout) != value:
                raise PlaywrightError('입력값이 유지되지 않았습니다')
        elif action == 'type':
            page.locator(selector).first.press_sequentially(value, delay=int(step.get('delay_ms', 30)),
                                                           timeout=timeout)
        elif action == 'click':
            target = page.locator(selector).first
            target.click(timeout=timeout, force=bool(step.get('force')))
            if step.get('until'):
                self.wait_next_page(step, target)
        elif action == 'select':
            page.locator(selector).first.select_option(value, timeout=timeout)
        elif action == 'check':
            # force: 디자인용 라벨이 덮고 있어도 체크 (이미 체크돼 있으면 아무것도 안 함)
            page.locator(selector).first.check(timeout=timeout, force=bool(step.get('force')))
        elif action == 'press':
            page.keyboard.press(value)
        elif action == 'wait_for':
            page.locator(selector).first.wait_for(state=step.get('state', 'visible'), timeout=timeout)
        elif action == 'click_first_available':
            # 선호 순서대로 나열한 후보 중 화면에 있는 첫 번째를 클릭 (예: 티타임)
            for candidate in step['selectors']:
                loc = page.locator(render(candidate, self.vars)).first
                if loc.count() and loc.is_visible() and loc.is_enabled():
                    loc.click(timeout=timeout)
                    log(f'  선택됨: {render(candidate, self.vars)}')
                    return
            raise PlaywrightError('선택 가능한 후보가 없습니다')
        elif action == 'assert_checked':
            # 후보 중 하나라도 체크돼 있으면 통과 (예: 원하는 골프장이 실제로 선택됐는지 확인)
            for candidate in step['selectors']:
                loc = page.locator(render(candidate, self.vars)).first
                if loc.count() and loc.is_checked():
                    return
            raise PlaywrightError('선택이 확인되지 않았습니다')
        elif action == 'wait_until_open':
            self.wait_until_open(step)
        elif action == 'pause':
            # 3D Secure OTP 등 사람이 직접 처리해야 하는 단계
            log(f'  수동 처리 대기: {step.get("message", "")}')
            if selector:
                page.locator(selector).first.wait_for(timeout=int(step.get('timeout_ms', 300000)))
            else:
                page.wait_for_timeout(int(step.get('timeout_ms', 60000)))
        elif action == 'screenshot':
            self.screenshot(step.get('name', 'step'))
        elif action == 'sleep':
            page.wait_for_timeout(int(step.get('ms', 500)))
        else:
            raise ValueError(f'알 수 없는 action: {action}')

    def run_step(self, idx, step):
        label = f'[{idx}] {describe(step, self.vars)}'
        # only_if_missing: 해당 요소가 이미 화면에 있으면 이 단계는 건너뜀 (예: 다음 달 버튼)
        if step.get('only_if_missing'):
            if self.page.locator(render(step['only_if_missing'], self.vars)).count():
                log(f'{label}  -> 이미 있음, 건너뜀')
                return True
        # skip_if_empty: 값이 비어 있으면 건너뜀 (예: CVV 를 .env 에 저장하지 않은 경우 직접 입력)
        if 'skip_if_empty' in step and not render(step['skip_if_empty'], self.vars):
            log(f'{label}  -> 값이 없어 건너뜀')
            return True
        # unless_checked: 후보 중 하나가 이미 체크돼 있으면 건너뜀 (불필요한 대기 방지)
        for candidate in step.get('unless_checked', []):
            loc = self.page.locator(render(candidate, self.vars)).first
            if loc.count() and loc.is_checked():
                log(f'{label}  -> 이미 선택됨, 건너뜀')
                return True
        if step.get('final') and self.dry_run:
            log(f'{label}  -> dry-run 이라 실행하지 않음')
            return False
        retry_seconds = float(step.get('retry_seconds', 0))
        deadline = time.time() + retry_seconds
        # 재시도 단계는 한 번 시도할 때 오래 기다리지 않는다
        attempt_timeout = int(step.get('attempt_timeout_ms', 1500)) if retry_seconds else None
        if step.get('optional') and not retry_seconds:
            attempt_timeout = int(step.get('attempt_timeout_ms', 3000))
        log(label)
        while True:
            try:
                self.do(step, attempt_timeout)
                return True
            except PlaywrightError as e:
                if time.time() < deadline:
                    if step.get('reload_on_retry'):
                        self.page.reload(wait_until='domcontentloaded')
                    else:
                        self.page.wait_for_timeout(int(step.get('retry_interval_ms', 300)))
                    continue
                if step.get('optional'):
                    log(f'  (optional) 건너뜀: {e.message.splitlines()[0]}')
                    return True
                raise

    def run(self, steps):
        for idx, step in enumerate(steps, 1):
            if self.run_step(idx, step) is False:
                return False
        return True


def add_dismiss_handlers(page, popups, variables):
    """언제 뜰지 모르는 안내 팝업이 클릭을 가로막으면 자동으로 닫는다."""
    def make_handler(name):
        def handler(locator):
            log(f'  팝업 닫기: {name}')
            locator.first.click()
        return handler

    for popup in popups:
        target = render(popup['selector'], variables)
        page.add_locator_handler(page.locator(target), make_handler(popup.get('name', target)))


def main():
    parser = argparse.ArgumentParser(description='Thriwe 골프 자동 예약')
    parser.add_argument('--config', default=str(BASE_DIR / 'config.toml'))
    parser.add_argument('--env', default=str(BASE_DIR / '.env'))
    parser.add_argument('--dry-run', action='store_true', help='final=true 단계(결제 확정)는 실행하지 않음')
    parser.add_argument('--now', action='store_true', help='오픈 시각을 기다리지 않고 바로 진행(테스트용)')
    parser.add_argument('--headless', action='store_true')
    parser.add_argument('--tee', help='티타임 지정. 예) 07:30, 14:10, 8:10AM (config 의 tee_* 값 대신 사용)')
    parser.add_argument('--days-ahead', type=int, help='오픈 날짜 기준 며칠 뒤를 예약할지 (config 값 대신 사용)')
    parser.add_argument('--course', help='골프장 이름 일부. 예) "Sharjah Golf & Shooting Club" (config 의 course 대신 사용)')
    parser.add_argument('--region', help='골프장 화면에서 먼저 고를 지역 라디오. 예) Sharjah (기본 지역이면 생략)')
    args = parser.parse_args()

    load_dotenv(Path(args.env))
    with open(args.config, 'rb') as f:
        cfg = tomllib.load(f)

    sched = cfg['schedule']
    tz = ZoneInfo(sched.get('timezone', 'Asia/Dubai'))
    open_at = dt.datetime.now(tz) if args.now else next_open_time(sched)
    if args.days_ahead is not None:
        sched['days_ahead'] = args.days_ahead
    if args.tee:
        cfg.setdefault('vars', {}).update(parse_tee(args.tee))
    if args.course:
        cfg.setdefault('vars', {})['course'] = args.course
    if args.region is not None:
        cfg.setdefault('vars', {})['region'] = args.region
    cfg.setdefault('vars', {}).setdefault('region', '')
    variables = build_vars(cfg, open_at)

    missing = [ENV_VARS[k] for k in ('email', 'password') if not variables[k]]
    if missing:
        sys.exit(f'환경변수가 없습니다: {", ".join(missing)} (.env 파일 확인)')

    offset = 0.0 if args.now else server_clock_offset(cfg['site']['base_url'])
    tee = f'{variables.get("tee_hour", "?")}:{variables.get("tee_minute", "?")} {variables.get("tee_ampm", "")}'
    region = f'{variables["region"]} / ' if variables.get('region') else ''
    log(f'오픈 시각 {open_at.isoformat()} / {region}골프장 {variables.get("course", "?")} / 예약 날짜 {variables["play_date"]} '
        f'/ 티타임 {tee} / dry-run={args.dry_run}')

    shot_dir = BASE_DIR / 'screenshots'
    shot_dir.mkdir(exist_ok=True)
    state_file = BASE_DIR / 'auth_state.json'
    browser_cfg = cfg.get('browser', {})

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=args.headless or browser_cfg.get('headless', False),
                                    channel=browser_cfg.get('channel') or None)
        context = browser.new_context(
            storage_state=str(state_file) if state_file.exists() and browser_cfg.get('reuse_login') else None,
            timezone_id=sched.get('timezone', 'Asia/Dubai'),
            viewport={'width': 1366, 'height': 900},
        )
        page = context.new_page()
        add_dismiss_handlers(page, cfg.get('dismiss', []), variables)
        runner = Runner(page, cfg, variables, open_at, offset, args.dry_run, shot_dir)
        ok, failed = False, False
        try:
            ok = runner.run(cfg['steps'])
            context.storage_state(path=str(state_file))
            log('완료' if ok else 'dry-run 종료 (결제 확정 직전에서 멈춤)')
        except Exception as e:  # noqa: BLE001 - 실패 원인을 스크린샷으로 남긴다
            failed = True
            log(f'실패: {e}')
            runner.screenshot('error')
        finally:
            runner.screenshot('final')
            if browser_cfg.get('keep_open_seconds'):
                page.wait_for_timeout(int(browser_cfg['keep_open_seconds']) * 1000)
            browser.close()
    sys.exit(1 if failed else 0)


if __name__ == '__main__':
    main()
