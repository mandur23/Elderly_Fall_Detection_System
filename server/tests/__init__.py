import logging

# 테스트 중 서버 로그가 결과 출력에 섞이지 않게 한다(로그 설정을 따로 하면 그대로 보인다).
logging.getLogger("server").addHandler(logging.NullHandler())
