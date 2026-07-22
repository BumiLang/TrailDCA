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
9. 시트 탭 이름이 "Sheet1"이 아니면 `GOOGLE_SHEET_TAB`을 수정하세요. 헤더 행은 첫 실행 시 자동으로 생성/보정됩니다 (`종목코드, 종목명, 마켓구분, 보유수량, 매입금액_원화, 수익률, 전략적용여부, 최고수익률, 익절기준, 청산여부, 소수점가능여부, 마지막갱신`).

## 4. 실행

```
python -m src.main
```

- 매일 KST 08:00 이후 최초 1회: 보유 종목을 조회해 시트와 동기화(신규 종목은 기본값으로 추가).
- 정규장 시간(한국 09:00-15:30, 미국 22:30-05:00경, 서머타임에 따라 변동) 동안 1초마다 수익률/최고수익률/익절기준을 갱신하고 전략에 따라 매수/청산을 시도합니다.
- 로그는 콘솔과 `traildca.log`에 동시에 남습니다.
- `Ctrl+C`(SIGINT)로 안전하게 종료됩니다 (진행 중인 주문 폴링을 마무리한 뒤 종료).

### Dry-run → Live 전환

1. `LIVE_TRADING=false`(기본값) 상태로 최소 하루 이상 돌려보고, 로그와 시트에 찍히는 `[DRY-RUN] BUY/SELL` 판단이 의도한 규칙과 일치하는지 확인하세요.
2. 문제 없으면 `.env`에서 `LIVE_TRADING=true`로 변경 후 재시작. 코드 변경은 필요 없습니다.
3. **주의**: dry-run에서는 실제 주문을 넣지 않으므로, 해외 종목의 소수점 거래 가능 여부(`소수점가능여부`)를 실제로는 확인할 수 없습니다. 이 값은 라이브 전환 후 각 종목의 첫 매수 시도에서 확정됩니다.

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
