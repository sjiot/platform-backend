# SJIOT 플랫폼/조립대 백엔드

## 1. 프로젝트 소개
- 키캡 커스터마이징 IOT 서비스
- CO-SHOW 2026 전시 목표

## 2. 시스템 아키텍처
- 전체 그림: Flutter 키오스크 → Flask(Cloud Run) → Redis(조회) / Mobius(기록)
- 왜 이런 구조인가: Redis가 단일 진실 소스, Mobius는 로깅/영속용 (COSS append-only라 직접 조회 불가)

## 3. API 문서
- Swagger UI 링크 ([/apidocs/](https://sjiot-backend-294910862364.asia-northeast1.run.app/apidocs/#/))
- 핵심 흐름 요약: 키오스크 주문 생성 → 빈 트레이 투입 → 키캡 배출 → 키캡 적재 → 보드 투입 → AGV 대기 장소 → AGV 픽업 → 조립대
- 백엔드 흐름 요약: 

## 4. 개발 환경 설정
- git clone, venv, pip install -r requirements.txt --break-system-packages
- 로컬 실행 (python app.py)
- env.yaml 예시
    - REDIS_URL
    - COLOR_LIST
    - BOARD_LIST
    - SWITCH_LIST
    - QUEUE_KEY
    - WAREHOUSE_KEY
    - STATION_KEY
    - STATION_VERIFIED_PREFIX
    - TEST_KEY_TTL
    - X-API-KEY
    - X-AUTH-CUSTOM-CREATOR
    - X-AUTH-CUSTOM-LECTURE
    - MP_URL
    - CB
    - AE_RN
    - MOBIUS_ORIGIN
    - MOBIUS_API_KEY (X-API-KEY 통일 필요)
    - ORDER_PAGE_BASE
    - ORDER_COUNTER_KEY
    - STATION_RESET_PASSWORD

## 5. 배포
- gcloud run deploy 명령어 그대로

## 6. 프로젝트 구조 (현재 수정 중)
config.py            ← env 전부. 다른 파일은 os.getenv 안 씀
services/
  extensions.py      ← redis 커넥션
  keys.py            ← 키 이름 + 생성 함수 (order_key(oid), station_order_key(sid)…)
  mobius.py          ← create_cin / get_latest_con (HTTP만)
  notification.py    ← sgn 봉투 파싱, vrq
  process.py         ← cnt_process 스냅샷 빌드 + push
domain/
  order_service.py   ← try_assign_next, dispatch, finish_order(아카이브·집계)
  station_service.py ← complete, 타임아웃
  stock_service.py   ← 카트리지 누적, 예약 계산, 품절
  validators.py
callbacks/           ← 위 레지스트리. 컨테이너 그룹별 파일
routes/
  order.py  station.py  stock.py  callback.py  admin.py

## 6. Redis 키 구조

## 7. 알려진 이슈 / TODO

## 8. 팀 / 담당
- 백엔드: lgk (전체 저장소 관리)
- 프론트: gsj, awj
- 브랜치 전략: main / lgk / gsj / awj / front_device / front_kiosk