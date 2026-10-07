# Thriwe 골프 자동 예약 (Emirates NBD)

`https://emiratesnbdbenefits.thriwe.com` 에서 **아부다비 시간 자정에 열리는 2주 뒤 골프 예약**을 자동으로 진행합니다.

흐름: 오픈 몇 분 전 로그인 → 코스 화면까지 이동 → 자정(Asia/Dubai, 사이트 서버 시간 기준)까지 대기 → 날짜·티타임 선택(열릴 때까지 재시도) → 카드 정보 입력 → 결제 → (3D Secure OTP 는 직접 입력) → 완료 화면 스크린샷

## 1. 설치

```bash
cd thriwe_booking
pip install -r requirements.txt
playwright install chromium
cp config.example.toml config.toml
cp .env.example .env        # 로그인/카드 정보 입력 (커밋되지 않음)
```

## 2. 실제 화면 요소 맞추기 (최초 1회, 필수)

`config.example.toml` 의 selector 는 **추정값**입니다. 실제 사이트에서 예약 과정을 한 번 녹화해 확인하세요.

```bash
playwright codegen https://emiratesnbdbenefits.thriwe.com/login
```

열린 브라우저에서 로그인 → 골프 → 코스 → 날짜 → 티타임 → 카드 입력 화면까지 진행하면
오른쪽 창에 `page.get_by_...` / `locator("...")` 코드가 생성됩니다. 그 selector 를 `config.toml` 의 각 step 에 옮기세요.
(결제 버튼은 누르지 마세요.)

## 3. 테스트

```bash
python book.py --now --dry-run      # 자정 대기 없이 바로, 결제 버튼 직전에서 멈춤
```

`screenshots/` 에 단계별 화면이 저장됩니다. 실패하면 `*_error.png` 로 어느 단계인지 확인하세요.
단, 아직 열리지 않은 날짜(2주 뒤)는 선택이 안 될 수 있으니 테스트 땐 `days_ahead` 를 13 등으로 낮추세요.

## 4. 스케줄 등록

스크립트가 아부다비 자정을 스스로 계산해 기다리므로, **자정 10분 전쯤 실행**만 되면 됩니다.
PC 가 켜져 있어야 하며 절전 모드가 되면 안 됩니다.

| PC 시간대 | 실행 시각 |
|---|---|
| 아부다비/두바이 (UTC+4) | 매일 23:50 |
| 한국 (UTC+9) | 매일 04:50 |

Windows (관리자 권한 명령 프롬프트, 경로는 본인 것으로):

```bat
schtasks /Create /TN "ThriweGolf" /SC DAILY /ST 23:50 /TR "C:\path\to\thriwe_booking\run_booking.bat"
```

특정 요일만: `/SC WEEKLY /D FRI /ST 23:50` (예: 금요일 자정 직전 → 2주 뒤 토요일 예약)

macOS / Linux (cron, 아부다비 시간대 PC 기준):

```cron
50 23 * * * cd /path/to/thriwe_booking && python3 book.py >> booking.log 2>&1
```

## 설정 요약 (`config.toml`)

| 항목 | 설명 |
|---|---|
| `schedule.open_time` / `timezone` | 예약 오픈 시각 (기본 `00:00:00`, `Asia/Dubai`) |
| `schedule.days_ahead` | 며칠 뒤 날짜를 예약할지 (기본 14) |
| `schedule.date_format` | 날짜 입력 형식 (`{play_date}` 에 적용) |
| `vars.course` 등 | step 에서 `{course}` 처럼 사용 |
| `steps` | 순서대로 실행할 동작 목록 |

step 동작: `goto`, `reload`, `fill`, `type`, `click`, `select`, `press`, `wait_for`, `click_first_available`(선호 티타임 순서), `wait_until_open`, `pause`(OTP 등 수동 처리), `screenshot`, `sleep`

step 옵션: `retry_seconds`(열릴 때까지 재시도), `reload_on_retry`, `optional`(실패해도 계속), `final`(`--dry-run` 시 실행 안 함)

## 주의사항

- 카드 정보는 `.env` 에만 두세요. 로그에는 `****` 로 가려집니다. `.env`, `config.toml`, `auth_state.json`, `screenshots/` 는 git 에서 제외됩니다.
- 카드 결제 시 3D Secure(OTP) 가 뜨면 자동화할 수 없습니다. 브라우저 창에서 직접 입력하세요. 스크립트가 완료 화면이 뜰 때까지 최대 5분 기다립니다.
- Thriwe 이용약관상 자동화 도구 사용이 제한될 수 있습니다. 노쇼 및 취소(48시간 전) 패널티도 있으니 결과를 꼭 확인하세요.
