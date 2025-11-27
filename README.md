# AI Coin Trader 대시보드

이 프로젝트는 Upbit KRW 마켓을 대상으로 LLM 기반 트레이딩 에이전트를 운영하면서, 포지션/거래 로그를 FastAPI 대시보드로 시각화하는 코드입니다.  


## 주요 기능
- **LLM 트레이딩**: `main.py`가 설정 파일(`configs/*.json`)을 읽어 모델별로 에이전트를 초기화하고, 포지션을 `data/agent_data/.../position/position.jsonl`에 기록합니다.
- **거래 도구**: `agent_tools`에 MCP 도구 모음이 있으며, Upbit 가격 조회, 매수/매도, paper trading 등을 FastMCP로 제공합니다.
- **시각화 대시보드** (`dashboard/`):
  - 총자산·현금·실현손익 카드
  - 현금 + 보유 코인의 포트폴리오 도넛 차트/legend
  - 보유 현황 테이블 (평단, 평가손익, 수익률)
  - 매수/매도 거래 내역 (체결시간, 단가, 거래금액)
  - 모델 의사결정 로그와 타임스탬프
  - API는 `/api/...`로 JSON 제공, `/web`에서 정적 UI 확인

## 커스텀/확장
- `prompts/agent_prompt_upbit.py` 를 수정해 LLM 행동을 세밀하게 제어할 수 있습니다.
- `UPBIT_BAR`, `UPBIT_BAR_MINUTES`, `START_CASH_KRW`, `SIGNATURE` 등은 `.env`로 관리합니다.
- Nginx + Certbot을 이용해 HTTPS로 서비스할 수 있습니다. (도메인 필요)

## 로그 및 데이터
- `data/agent_data/<signature>/position/position.jsonl`: 모든 포지션/거래 스냅샷 (KST `timestamp` 포함)
- `data/agent_data/<signature>/log/YYYY-MM-DD/log.jsonl`: LLM reasoning 로그
- 대시보드에서는 해당 파일을 기반으로 현재 상태를 실시간 계산합니다.

## 주의사항
- 실제 계좌 연결 시 `UPBIT_DRY_RUN=false`, API 키, 보안 설정을 반드시 확인하세요.
- 무료 도메인(kro.kr 등)은 Let’s Encrypt 발급 제한이 자주 걸리니, 가능하면 독자 도메인 사용을 권장합니다.
- MCP/uvicorn 프로세스는 `Ctrl+C`로 종료된다. 변경 사항 적용 시 `bash main.sh` 재실행이 필요합니다.

## 문의
피드백과 후원은 대시보드 하단 Buy Me A Coffee 버튼 또는 Communication.md의 안내를 참고해 주세요.
