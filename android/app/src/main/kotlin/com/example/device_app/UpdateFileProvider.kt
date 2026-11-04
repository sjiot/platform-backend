package com.example.device_app

import androidx.core.content.FileProvider

/**
 * 원격 업데이트 APK 전달용 FileProvider.
 *
 * 다른 플러그인이 androidx FileProvider를 manifest에 선언해도
 * 이름이 겹치지 않도록 하위 클래스로 따로 둔다.
 */
class UpdateFileProvider : FileProvider()
