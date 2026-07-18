# EWY·코스피 조정 모니터

EWY 가격 구조, 한국 투자자 수급, 주봉 모멘텀, 환율, 반도체와 글로벌 위험지표를 함께 추적하는 규칙 기반 대시보드입니다. 매 영업일의 판정과 근거를 JSON 이력으로 남기고, 새 시장 데이터가 확인됐을 때만 핵심 요약을 Telegram으로 보냅니다.

**대시보드:** <https://jinhae8971.github.io/kospi-regime-brief/>

> 이 프로젝트의 확률은 과거 표본으로 보정된 예측확률이 아니라 공개된 규칙의 증거점수를 5~95% 범위로 변환한 추적지표입니다. 투자자문이 아닙니다.

## 무엇을 추적하나

최종 점수는 `-100~+100`이고, 양수일수록 조정 막바지 증거가 강하다는 뜻입니다.

```text
규칙 기반 막바지 추정확률 = clamp(5, 95, 50 + 0.45 × 증거점수)
조정 진행 확률 = 100 - 막바지 추정확률
```

| 요인 | 최대 가중치 | 주요 판단 근거 |
|---|---:|---|
| 가격 구조 | 20 | EWY 153·155.58 지지, 169~172·177.5·184.2·192.25 회복선 |
| 단기 반전 | 15 | 5일선, higher low, 일봉 MACD, 거래량과 봉의 종가 위치 |
| 주봉 모멘텀 | 15 | 완료된 금요일 봉의 Wilder RSI와 MACD 교차·히스토그램 방향; 진행 중 주봉은 점수에서 제외 |
| 외국인 수급 | 15 | 코스피 외국인 3일·10일 누적과 연속 순매수/순매도 |
| 원/달러 | 10 | USD/KRW 5일 변화와 20일선 방향 |
| 반도체 | 10 | SOXX, 삼성전자·SK하이닉스의 코스피 대비 상대강도 |
| 글로벌 위험 | 10 | VIX, 광의 달러, 미국 10년물 |
| 파동 완성도 | 5 | 수동 ABC 앵커의 C/A 비율; 주관성을 감안해 낮은 가중치 적용 |

판정 구간은 다음과 같습니다.

| 막바지 추정확률 | 판정 |
|---:|---|
| 68~95% | 막바지 가능성 높음 |
| 56~67% | 막바지 우세 |
| 45~55% | 재시험·방향 확인 구간 |
| 33~44% | 조정 진행 중 우세 |
| 5~32% | 추가 조정 가능성 높음 |

누락된 요인의 가중치는 다른 요인에 재분배하지 않고 0점으로 둡니다. 가용 가중치가 90% 이상이면 신뢰도 높음, 75~89%는 보통, 75% 미만은 낮음으로 표시합니다.

## 공개 데이터 소스

| 데이터 | 공개 소스 | 사용 방식 |
|---|---|---|
| EWY·SOXX 가격과 거래량 | Nasdaq 공개 quote API | 일봉, 현재 관측치, 기술지표 |
| 코스피·삼성전자·SK하이닉스 | 네이버 모바일 증권 공개 응답 | 한국장 가격과 상대강도 |
| 외국인·기관·개인 순매수 | 네이버 모바일 증권의 KRX 투자자 동향 | 일·3일·5일·10일 누적, 단위는 조원 |
| USD/KRW | 네이버 기준환율 + Open Exchange Rates 최신 관측 | 연속 일별 환율을 1차로 사용하고 최신 관측일 보완 |
| USD/KRW 대체 시계열 | FRED CSV (`DEXKOUS`) | 네이버 기준환율 수집 실패 시 fallback |
| VIX, 미국 10년물, 광의 달러 | FRED CSV (`VIXCLS`, `DGS10`, `DTWEXBGS`) | 글로벌 위험 점수와 기준일 확인 |

각 스냅숏은 실제 관측일과 출처 URL을 함께 저장합니다. 한국과 미국의 휴장일이 다르므로 `market_dates.kr`과 `market_dates.us`를 분리하며, 화면의 생성 시각을 시장 데이터 시각으로 오해하지 않아야 합니다.

## 저장 데이터 계약

GitHub Pages와 테스트는 아래 세 파일을 공통 계약으로 사용합니다.

- `data/latest.json`: 최신 전체 지표, 판정, 요인별 점수, 시계열과 출처
- `data/history.json`: `US-YYYY-MM-DD_KR-YYYY-MM-DD` 시장 키별 축약 이력; 같은 키는 덮어쓰고 최근 730개 유지
- `data/methodology.json`: 점수 공식, 버전, 가격선과 한계
- `data/notification.json`: Telegram 전송에 성공한 마지막 시장 키와 전송 시각; 실패한 시장일을 다음 일정에서 재시도하기 위한 운영 상태

`schema_version`과 `methodology_version`이 바뀌면 대시보드와 테스트도 함께 갱신해야 합니다. GitHub Actions는 오직 `data/*.json`만 자동 커밋하며 커밋 메시지에 `[skip ci]`를 붙입니다. `push` 트리거에서도 `data/**`를 제외해 자동 커밋이 다시 워크플로를 실행하는 루프를 막습니다.

## 자동 실행과 Telegram

예약 실행은 평일 **07:17 KST**입니다. GitHub cron 표현은 `17 22 * * 0-4`이며 UTC 일~목요일에 실행됩니다. 07시대로 둔 이유는 미국 동절기 종가와 데이터 공급자의 반영 시간을 확보하기 위해서입니다.

워크플로 순서는 다음과 같습니다.

1. 네트워크를 사용하지 않는 계약 테스트 실행
2. `monitor.py --dry-run --github-output`으로 데이터 수집 및 JSON 생성
3. 생성된 `data/*.json`만 커밋하고, 원격 경합 시 강제 푸시 없이 fetch/rebase 후 재시도
4. `GITHUB_TOKEN` 커밋도 반영되도록 Pages build API를 명시적으로 호출
5. 공개 Pages의 `data/latest.json`이 같은 시장 키와 `generated_at`을 제공하는지 확인
6. 마지막 성공 알림과 다른 시장 키일 때만 Telegram을 보내고, 성공 후 `data/notification.json`만 별도 커밋

### 필요한 Secrets

저장소의 **Settings → Secrets and variables → Actions**에 다음 repository secret을 등록합니다.

- `TELEGRAM_TOKEN`: BotFather에서 발급한 봇 토큰
- `TELEGRAM_CHAT_ID`: 메시지를 받을 개인·그룹·채널 ID

토큰이나 채팅 ID를 코드, JSON, Actions 로그에 넣지 마세요. 두 secret이 없으면 데이터·페이지 갱신은 가능하지만 Telegram 전송은 실패합니다. 실행 실패 알림도 secret이 둘 다 있을 때만 별도로 전송됩니다.

이 저장소의 GitHub Pages는 `main` 브랜치의 `/root`를 직접 배포하는 branch-source 방식입니다. GitHub 문서상 `GITHUB_TOKEN`으로 만든 커밋은 Pages 빌드를 자동 시작하지 않으므로, 데이터 job이 `POST /repos/{owner}/{repo}/pages/builds`를 호출해 재빌드를 요청한 뒤 공개 JSON의 시장 키와 생성시각을 함께 확인합니다. 별도의 Pages artifact 배포나 개인 액세스 토큰은 사용하지 않습니다. 조직 정책이 `GITHUB_TOKEN` 쓰기를 제한하면 **Settings → Actions → General → Workflow permissions**에서 `contents: write`와 `pages: write`가 허용되는지도 확인합니다.

### 수동 실행

Actions의 **EWY correction monitor → Run workflow**에서 다음 입력을 사용할 수 있습니다.

| 입력 | 기본값 | 동작 |
|---|---:|---|
| `notify` | `false` | 새 시장일을 발견한 경우 Telegram 전송을 허용 |
| `force_send` | `false` | `notify=true`와 함께 사용하면 동일 시장일도 테스트 재전송 |

두 입력을 모두 끄면 데이터·이력만 안전하게 갱신하고 branch-source Pages가 이를 반영합니다. 코드 변경을 `main`에 push해도 동일한 테스트·수집·Pages 갱신을 수행하며, 최종 커밋 메시지에 `[notify]`가 있으면 공개된 최신 스냅숏으로 Telegram 연결을 한 번 테스트합니다.

## 로컬 검증

Python 3.11 이상이 필요합니다.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt -r requirements-dev.txt
python -m pytest -q
python monitor.py --dry-run
```

`--dry-run`은 외부 데이터를 읽어 로컬 JSON을 갱신하지만 Telegram은 보내지 않습니다. 테스트 자체는 저장된 fixture와 JSON만 사용하므로 네트워크가 없어도 실행됩니다.

## 운영 한계

- Nasdaq·네이버·FRED의 공개 응답은 공식 유료 피드가 아니며 지연, 형식 변경, 차단 또는 사후 정정이 있을 수 있습니다.
- GitHub 예약 작업은 정확히 07:17에 시작된다는 보장이 없고, GitHub 장애나 public repository의 장기 무활동으로 누락될 수 있습니다.
- GitHub Pages는 공개 사이트입니다. JSON에는 공개 시장 데이터와 계산 결과만 두고 개인정보나 secret을 저장하면 안 됩니다.
- EWY는 코스피 자체가 아니라 한국 주식과 USD/KRW 효과를 함께 반영합니다.
- 엘리엇 파동의 220.09→174.45(A)→192.25(B) 앵커와 지지·확인선은 수동 가설입니다. 시장 구조가 바뀌면 방법론 버전을 올려 재검토해야 합니다.
- 진행 중인 주봉은 점수에서 제외하고 마지막 완료 금요일 봉을 사용합니다. 따라서 주중 급변은 일봉 요인에 먼저 반영되며 주봉 요인은 다음 완료 봉까지 유지됩니다.
- 한국·미국의 휴장 불일치와 공급자별 최신일 차이 때문에 단일 시점의 수치보다 이력과 `status.freshness`를 함께 봐야 합니다.
- 현재 규칙은 과거 성과로 보정된 통계 모델이 아니며 미래 수익이나 손실을 보장하지 않습니다.
