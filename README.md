# TrailDCA

토스증권 OpenAPI + 구글 스프레드시트로 돌아가는 DCA / 트레일링 익절 자동매매 봇.

**기본값은 dry-run(모의투자)입니다.** `.env`의 `LIVE_TRADING=true`로 바꾸기 전까지는 실제 주문이 절대 나가지 않습니다.

## 1. 설치

```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## 2. Toss Securities OpenAPI 설정

1. 토스증권 WTS(영업점) 설정에서 Open API `client_id`/`client_secret` 발급.
2. **접근 허용 IP 등록 필수.** 이 봇을 실행하는 PC/서버의 공인 IP를 WTS Open API IP 화이트리스트에 등록해야 호출이 성공합니다. 배포 위치(로컬 PC ↔ 서버)를 바꾸면 IP도 다시 등록해야 합니다.
3. `.env`에 `TOSS_CLIENT_ID`, `TOSS_CLIENT_SECRET` 입력.
4. 계좌가 여러 개면 `TOSS_ACCOUNT_SEQ`를 지정하세요. 비워두면 `GET /api/v1/accounts` 응답의 첫 번째 계좌를 사용합니다.

## 3. 구글 스프레드시트 서비스 계정 설정

사람 로그인 없이 백그라운드에서 상시 접근하려면 서비스 계정이 필요합니다.

1. [Google Cloud Console](https://console.cloud.google.com/)에서 프로젝트 생성 (또는 기존 프로젝트 사용).
2. **API 및 서비스 → 라이브러리**에서 "Google Sheets API" 검색 후 사용 설정.
3. **API 및 서비스 → 사용자 인증 정보 → 사용자 인증 정보 만들기 → 서비스 계정** 생성.
4. 생성된 서비스 계정 → **키** 탭 → **키 추가 → 새 키 만들기 → JSON** 다운로드.
5. 다운로드한 JSON 파일을 프로젝트 루트에 `service_account.json`으로 저장 (이미 `.gitignore`에 포함되어 커밋되지 않음).
6. JSON 파일 안의 `client_email` 값(예: `xxx@yyy.iam.gserviceaccount.com`)을 복사.
7. 대상 스프레드시트를 열어 **공유** → 위 `client_email`을 **편집자**로 추가.
8. `.env`의 `GOOGLE_SHEET_ID`는 이미 시트 URL에서 추출되어 기본값으로 채워져 있습니다:
   `1OP20AeVjn8g6WIB65uLB49reioEHOnnjff9sa3q0TQw`
9. 시트 탭 이름이 "Sheet1"이 아니면 `GOOGLE_SHEET_TAB`을 수정하세요. 헤더 행은 첫 실행 시 자동으로 생성/보정됩니다 (`종목코드, 종목명, 마켓구분, 보유수량, 매입금액_원화, 수익률, 전략적용여부, 최고수익률, 익절기준, 청산여부, 마지막갱신`).

## 4. 실행

```
python -m src.main
```

- 매일 KST 08:00 이후 최초 1회: 보유 종목을 조회해 시트와 동기화(신규 종목은 기본값으로 추가).
- 정규장 시간(한국 09:00-15:30, 미국 22:30-05:00경, 서머타임에 따라 변동) 동안 1초마다 수익률/최고수익률/익절기준을 갱신합니다.
- 실제 매수/매도 **주문**은 장 시작 즉시가 아니라 시장·방향별로 정해진 지연 시간이 지나야 나갑니다 (`src/config.py`의 `KR_BUY_DELAY_AFTER_OPEN`=1시간, `KR_SELL_DELAY_AFTER_OPEN`=5분, `US_BUY_DELAY_AFTER_OPEN`/`US_SELL_DELAY_AFTER_OPEN`=0, 즉 미국은 장 시작과 동시). 지연 중에도 최고수익률/익절기준 계산 자체는 계속 진행되므로, 지연이 끝나는 시점엔 이미 정확한 값을 갖고 있습니다.
- 이 지연 판단은 로컬 PC 시계가 아니라, Toss API 응답의 `Date` 헤더로 보정한 시각(`TossClient.now()`)을 기준으로 합니다 — PC 시계가 실제와 몇 초/몇 분 어긋나 있어도 장 시작 후 경과 시간 계산에는 영향이 없습니다.
- 시트 쓰기는 매 틱마다 나가지 않고 `SHEET_FLUSH_INTERVAL_SECONDS`(기본 1초, `src/config.py`)마다 그 사이 누적된 변경분을 모아 한 번에 씁니다 — Google Sheets API의 분당 쓰기 요청 한도를 넘기지 않기 위함입니다. 값을 늘리면(예: 5초) 요청 빈도가 더 줄어듭니다.
- 로그는 콘솔과 `traildca.log`에 동시에 남습니다.
- `Ctrl+C`(SIGINT)로 안전하게 종료됩니다 (진행 중인 주문 폴링을 마무리한 뒤 종료, 아직 시트에 못 쓴 변경분도 종료 직전 한 번 더 flush).

### 전략 요약 (`src/config.py`, `src/strategy.py`)

- **DCA 매수**: 종목당 누적 매입금액이 10만원(`DAILY_BUY_TARGET_KRW`) 미만이면 수익률과 무관하게 매일 5,000원(`DAILY_BUY_KRW`)씩 매수. 10만원 이상이면 수익률이 10%(`DAILY_BUY_RESUME_RATE`) 이상일 때만 계속 매수.
- **익절기준(트레일링 스탑)**: 최고수익률(peak)이 10%(`PEAK_ACTIVATION_RATE`)에 도달하면 활성화되어 정확히 5%에서 시작하고, peak 30%(`TAKE_PROFIT_BREAKPOINT`)까지는 선형으로 증가해 21%에 도달, 그 이후로는 `peak × 0.7`(`TAKE_PROFIT_HIGH_SLOPE`)로 계산됩니다. 최고수익률은 절대 내려가지 않고, 활성화 전에는 익절기준이 갱신되지 않습니다.
- **청산**: 최고수익률이 10% 이상이고 현재 수익률이 익절기준 이하로 떨어지면 전량 매도. 단, 누적 매입금액이 10만원(`DAILY_BUY_TARGET_KRW`) 미만이면 이 조건을 만족해도 청산을 생략합니다 — 아직 DCA 목표 금액을 다 채우지 못한 포지션을 일시적인 하락으로 통째로 정리하지 않기 위함이며, 포지션이 다 갖춰진 뒤에야 청산이 가능해집니다.
- **소수점 매수 미지원 종목**: 5,000원 금액 주문이 실패하면 1주 통매수로 폴백합니다. "1주를 추가로 샀을 때 예상되는 수익률"(매수 *후* 평단가가 밀리면서 낮아질 값, `projected_rate`)을 기준으로 매입금액 구간별로 다르게 판단합니다:
  - **현재 매입금액 < 10만원 & 예상 누적 매입금액 < 13만원 (`DAILY_BUY_TARGET_KRW`/`NONFRACTIONAL_DCA_CEILING_KRW`)**: DCA 유예구간 — 수익률과 무관하게 무조건 매수 (소수점 지원 종목의 "10만원 미만 무조건 매수"와 같은 취지 — 1주 단위라 10만원을 한 번에 넘길 수 있어 여유분(13만원)을 둠). 이 구간의 매수는 최고수익률을 리셋하지 않습니다 — 수익률 게이팅 없이 강제로 사는 날마다 그날의 (자칫 낮을 수 있는) 수익률로 최고수익률을 덮어쓰면, 아직 다 쌓이지도 않은 포지션의 트레일링 스탑 추적이 매일 망가지기 때문입니다.
  - **현재 매입금액 < 10만원 & 예상 누적 매입금액 ≥ 13만원**: `projected_rate ≥ 10%(PEAK_ACTIVATION_RATE)`이면 진입 — 익절기준이 10%보다 높게 올라가 있어도 이 구간에서는 그 높은 기준을 요구하지 않습니다.
  - **현재 매입금액 ≥ 10만원**: `projected_rate ≥ max(10%, 현재 익절기준)`이어야 진입.

  뒤의 두 구간(유예구간 제외)에서 매수가 체결되면 최고수익률은 이 `projected_rate` 값으로 그대로(직전 최고수익률과 비교하지 않고) 덮어씁니다.

  이 전략이 관리하는 종목은 이미 포지션이 있다는 전제이며, 최초 진입(첫 매수)은 수동으로 하는 것을 가정합니다.

### Dry-run → Live 전환

1. `LIVE_TRADING=false`(기본값) 상태로 최소 하루 이상 돌려보고, 로그와 시트에 찍히는 `[DRY-RUN] BUY/SELL` 판단이 의도한 규칙과 일치하는지 확인하세요.
2. 문제 없으면 `.env`에서 `LIVE_TRADING=true`로 변경 후 재시작. 코드 변경은 필요 없습니다.
3. **주의**: dry-run에서는 실제 주문을 넣지 않으므로, 종목별 소수점 거래 지원 여부에 따라 실제 매수가 5,000원어치 금액 주문으로 체결될지 1주 매수로 폴백될지는 라이브 전환 후에만 확인할 수 있습니다.

## 5. 상시 구동 관련

- 보유 종목에 미국 주식이 있으면 하루 최대 20시간 이상(한국장 09:00~15:30 + 미국장 22:30~05:00경) 프로세스가 떠 있어야 합니다.
- 이 프로젝트는 순수 Python 장기 실행 프로세스로 되어 있어 어디서나 `python -m src.main`으로 실행 가능합니다. OS 서비스 등록(Windows 작업 스케줄러의 "로그온 여부와 상관없이 실행" 옵션 + 절전 모드 해제, 또는 리눅스 systemd 등)은 배포 환경이 정해진 뒤 별도로 구성하세요.
- 크래시 시 자동 재시작이 필요하면 배포 환경에 맞는 프로세스 매니저(Task Scheduler 재시도, systemd `Restart=on-failure`, supervisord 등)를 추가로 구성하세요. 현재 코드는 재시작되어도 `state.json`과 시트의 값으로 상태를 복구합니다 (단, 재시작 당일 아직 매수를 시도하지 않았다면 다시 시도합니다 — Toss `clientOrderId` 멱등성 키로 중복 주문 자체는 방지됩니다).

## 6. 테스트

```
pytest tests/ -v
```

전략 규칙(`src/strategy.py`)에 대한 순수 함수 유닛테스트만 포함되어 있습니다. 실거래/시트 연동은 위 dry-run 절차로 직접 검증하세요.

## 보안

- `.env`, `service_account.json`, `state.json`, `traildca.log`는 모두 `.gitignore`에 포함되어 커밋되지 않습니다.
- 이 프로젝트를 만들며 대화창에 붙여넣었던 `TOSS_CLIENT_ID`/`TOSS_CLIENT_SECRET`는 평문으로 전달되었으므로, WTS Open API 설정에서 로테이션(재발급)하는 것을 권장합니다.
