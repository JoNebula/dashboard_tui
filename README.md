dashboard_tui: 경량 서버 모니터링 대시보드

==================================================

프로젝트 개요

dashboard_tui는 서버 관리를 위해 설계된 미니 터미널 사용자 인터페이스 (TUI) 기반 대시보드입니다.
매우 낮은 시스템 부하(Very Small Load)로 동작하며, 시스템의 핵심 리소스 정보를 실시간으로 한눈에 파악할 수 있게 해줍니다.

--------------------------------------------------

주요 기능

- 실시간 모니터링: CPU, GPU, RAM, Disk, Processor 정보 등을 지속적으로 업데이트합니다.
- 저부하 설계: 시스템 자원을 최소한으로 사용하여 모니터링 자체로 인한 성능 저하를 방지합니다.
- 프로세스 관리: sudo 권한으로 실행 시, 다른 사용자의 프로그램을 종료하는 등 서버 관리 기능을 수행할 수 있습니다.

<img width="1557" height="966" alt="image" src="https://github.com/user-attachments/assets/9607d241-f9e3-4244-894c-94965f1882ff" />

--------------------------------------------------

설치 및 실행 가이드

이 프로젝트는 Conda 환경을 기반으로 설정하는 것을 권장합니다.

You can use as managing program(quit other's program) if you start program with "sudo" authority.

$ conda env create -f environment.yml
$ conda activate manager
$ sudo $(which python) mini_dash_tui.py
