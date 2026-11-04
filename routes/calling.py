from flask import Blueprint, jsonify

from services.calling import get_display_status

bp = Blueprint('calling', __name__)

@bp.route('/display/status', methods=['GET'])
def display_status():
    """
    호출 화면 표시 상태 조회
    ---
    tags:
      - Display
    responses:
      200:
        description: 조립대별 호출 칸과 대기 번호. 주문이 없어도 200
        schema:
          type: object
          properties:
            stations:
              type: array
              description: 모든 조립대. 표시할 주문이 없으면 order_seq null, status idle
              items:
                type: object
                properties:
                  station_id:
                    type: integer
                    example: 1
                  order_seq:
                    type: integer
                    example: 12
                  status:
                    type: string
                    enum: [calling, assembling, idle]
                    example: calling
            waiting:
              type: array
              description: 대기 번호. order_seq 오름차순
              items:
                type: integer
              example: [13, 14, 15]
    """
    return jsonify(get_display_status()), 200
