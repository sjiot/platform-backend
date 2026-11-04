# 유사 논문과 작성 추천

- 작성: 2026-11-04
- 기준 양식: 한국통신학회(KICS) 학부논문 트랙. A4 1~2쪽, 포스터 패널 100×200 cm. 다른 학회에 내면 양식부터 다시 확인한다.

## 1. 유사 논문

"키캡 조립 라인 + oneM2M + 서버 지연 개선"을 통째로 다룬 선행 논문은 국내외에서 찾지 못했다. 아래 논문은 각각 한 부분씩 겹친다. 가까운 순서다.

| 논문 | 겹치는 부분 | 가져올 점 |
|---|---|---|
| 유태람 외, oneM2M 표준 IoT 플랫폼 기반 디지털 트윈 관제 시스템, KICS 2026 동계, 세종대 ([PDF](https://d2j16w31g89z0j.cloudfront.net/site/2026w/abs/0778-GAVDJ.pdf)) | Mobius 구독 기반 제어, 지연·전송률 측정, 학부 포스터 2쪽 | 요약·서론·시스템 설계·실험·결론 구성, 그림 3개와 표 1개 배치, 표 형식(방식/방향/전송률/지연) |
| Troscia 외, Scalable OneM2M IoT Open-Source Platform Evaluated in an SDN Optical Network Controller Scenario, Sensors 2022 ([PMC](https://pmc.ncbi.nlm.nih.gov/articles/PMC8778035/)) | Mobius 구독 알림이 가입자에게 도달하는 시간 측정 | 25·50·75 백분위 표, 조건당 30회 반복, 구독자 수와 갱신 주기별 지연 그래프 |
| Pranavasri 외, Exploratory Study of oneM2M-based Interoperability Architectures for IoT, ICSA-C 2024 ([ICSA](https://conf.researchr.org/details/icsa-2024/icsa-2024-software-architecture-in-practice-track/7/Exploratory-Study-of-oneM2M-based-Interoperability-Architectures-for-IoT-A-Smart-Cit)) | Mobius 응답 시간·CPU를 부하별로 측정 | 실험 환경을 CPU·RAM·OS까지 적는 방식, p95와 최대 RPS를 한 표에 두는 방식, 로그 축 그래프 |
| Choi 외 (KETI), Towards Real-Time Data Delivery in oneM2M Platform for UAV Management System, ICEIC 2019 ([dblp](https://dblp.org/rec/conf/elinfocom/ChoiAPK19)) | oneM2M 전달 경로를 바꿔 지연을 줄이고 전후 비교 | "플랫폼 전달 구조 개선 → 지연 감소" 논리 전개, 서론 인용 |
| Um 외, Industrial Device Monitoring and Control System based on oneM2M for Edge Computing, IEEE SSCI 2018 ([SKKU](https://pure.skku.edu/en/publications/industrial-device-monitoring-and-control-system-based-on-onem2m-f/)) | Mobius 기반 스마트팩토리 장비 제어 구조 | 서론에서 "oneM2M 기반 제조 제어" 선행 연구로 인용 |
| 오현수 외, Omniverse 플랫폼과 IoT 플랫폼 연동에 관한 연구, KICS 2022 하계 10A-4 / 박민용 외, 인공지능 학습데이터 확장 기능을 제공하는 oneM2M 표준 기반 사물인터넷 플랫폼 개발, KICS 2022 하계 30F-93 ([프로그램](https://d2j16w31g89z0j.cloudfront.net/site/2022s/2022+Summer+Paper2.pdf)) | 같은 학교의 oneM2M 포스터 | 연구실 선행 연구로 참고문헌에 넣기 |

### 선행 논문 대비 차별점

| 선행 논문 | 이 논문 |
|---|---|
| 플랫폼 전체의 지연 또는 전달 방식(구독/폴링) 비교 | 제어 서버 안의 구간별 지연을 나눠 병목을 찾음 |
| 평균 하나로 보고 | p50, p95, 실패율, 표본 수를 함께 보고 |
| 플랫폼 성능 자체를 평가 | 같은 플랫폼에서 서버 쪽 호출 방식을 바꿔 전후 비교 |

### 확인하지 못한 것

- ICEIC 2019 논문은 서지 정보만 확인했고 초록 원문은 열지 못했다.
- KICS 2025 추계 프로그램은 일부만 읽어 더 가까운 논문이 있을 수 있다.
- 유태람 논문은 전후 비교가 아니라 두 전달 방식의 비교다. 개선 전후 비교 형식은 Choi 외 논문을 참고한다.

## 2. 범위 결정

2쪽에는 주장 하나만 들어간다. [README.md](README.md)의 후보 A·C·E를 다 넣지 않는다.

| 데이터 상황 | 범위 |
|---|---|
| A, E 모두 실측 완료 | A(연결 재사용) + E(막힌 주문 건너뛰기). 통신 하나, 정책 하나 |
| A만 완료 | A 하나. 결론은 측정으로 찾은 병목 |
| 효과가 작음 | 그대로 쓰고 구간 분해 결과를 중심으로 |

## 3. 구성

유태람 논문의 틀에 지금 자료를 넣으면 다음과 같다.

| 절 | 분량 | 내용 | 출처 |
|---|---|---|---|
| 제목 | 1줄 | oneM2M 기반 키캡 조립 IoT 라인 제어 서버의 통신 지연 분석 및 개선 | [05-poster-outline.md](05-poster-outline.md) 첫 후보 |
| 요약 | 5문장 | 라인 구성, 문제(콜백 한 건 안에서 Redis와 Mobius 호출이 직렬), 개선 내용, 측정 방법, 결과 수치 하나 | |
| 서론 | 1단락 | oneM2M 기반 제어 선행 연구 2건(Um 외, Choi 외) 인용. 선행 연구는 플랫폼 지연만 봤고 제어 서버 안의 구간별 병목은 다루지 않았다는 차별점 | |
| 시스템 설계 | 그림 1 + 1단락 | 구성도, 명령·보고 경로 5단계. 주문 단계 목록은 뺀다 | [01-system.md](01-system.md) |
| 개선 구현 | 2~3문장 | 무엇을 바꿨는지. 코드 이름은 적지 않는다 | [03-improvements.md](03-improvements.md) |
| 실험 | 환경 1줄 + 표 1 + 그림 2 | 환경은 서버 사양, Mobius 위치, Redis 위치. 표 1은 구간별 전후 p50·p95·실패율·표본 수. 그림 2는 전후 CDF 또는 묶음 막대 | [04-experiments.md](04-experiments.md) 8장 |
| 결론 | 3줄 | 줄어든 구간과 크기 두 줄, 한계 한 줄(공용 Mobius, 표본 수) | |

### 표 1 틀

유태람 논문의 표 형식을 전후 비교로 바꾼 것이다.

| 구간 | 조건 | 표본 | p50 (ms) | p95 (ms) | 실패율 |
|---|---|---:|---:|---:|---:|
| Mobius 쓰기 | 기존 | | | | |
| Mobius 쓰기 | 개선 | | | | |
| 콜백 처리 | 기존 | | | | |
| 콜백 처리 | 개선 | | | | |

E를 넣으면 [04-experiments.md](04-experiments.md) 8장의 배정 정책 표를 표 2로 쓴다.

## 4. 포스터 패널

- 구성도를 가장 크게 둔다.
- 결과표는 하나만 둔다.
- 결론은 세 줄로 적는다.
- 본문 문장은 거의 옮기지 않는다.

## 5. 표현

실측 전 수치를 쓰지 않는 원칙과 "실측 시간 분포를 넣은 시뮬레이션 결과" 같은 표기 구분은 [05-poster-outline.md](05-poster-outline.md)의 표현 주의 표를 따른다.

## 6. 쓰기 전에 할 일

표 1에 들어갈 값이 하나도 없다. [04-experiments.md](04-experiments.md) 1장의 리눅스 서버 기준 데이터를 먼저 모으고, A 실험 결과가 나온 뒤 2장의 범위를 확정한다.
