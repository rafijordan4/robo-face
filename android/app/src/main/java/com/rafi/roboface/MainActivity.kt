package com.rafi.roboface

import android.Manifest
import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Color
import android.media.AudioManager
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.speech.RecognitionListener
import android.speech.RecognizerIntent
import android.speech.SpeechRecognizer
import android.view.View
import android.view.WindowManager
import android.webkit.JavascriptInterface
import android.webkit.WebView
import org.json.JSONObject

class MainActivity : Activity() {

    private lateinit var web: WebView
    private var recognizer: SpeechRecognizer? = null
    private var micOn = false
    private val handler = Handler(Looper.getMainLooper())
    private var audio: AudioManager? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        audio = getSystemService(AUDIO_SERVICE) as AudioManager

        web = WebView(this)
        web.setBackgroundColor(Color.BLACK)
        web.settings.javaScriptEnabled = true
        web.settings.domStorageEnabled = true
        web.settings.mediaPlaybackRequiresUserGesture = false
        web.addJavascriptInterface(Bridge(), "Android")
        setContentView(web)
        web.loadUrl("file:///android_asset/face.html")
        hideSystemUi()

        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(arrayOf(Manifest.permission.RECORD_AUDIO), 1)
        }
    }

    @Suppress("DEPRECATION")
    private fun hideSystemUi() {
        window.decorView.systemUiVisibility = (View.SYSTEM_UI_FLAG_FULLSCREEN
                or View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                or View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
                or View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
                or View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION
                or View.SYSTEM_UI_FLAG_LAYOUT_STABLE)
    }

    override fun onWindowFocusChanged(hasFocus: Boolean) {
        super.onWindowFocusChanged(hasFocus)
        if (hasFocus) hideSystemUi()
    }

    private fun js(code: String) {
        web.post { web.evaluateJavascript(code, null) }
    }

    // ---- bridge dipanggil dari JS ----
    inner class Bridge {
        @JavascriptInterface
        fun startMic() {
            runOnUiThread {
                if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
                    requestPermissions(arrayOf(Manifest.permission.RECORD_AUDIO), 1)
                    js("onMicError('Izinkan mic dulu, lalu klik dua kali lagi')")
                    return@runOnUiThread
                }
                if (!SpeechRecognizer.isRecognitionAvailable(this@MainActivity)) {
                    js("onMicError('Layanan suara Google tidak ada di HP ini')")
                    return@runOnUiThread
                }
                micOn = true
                muteBeep(true)
                listen()
            }
        }

        @JavascriptInterface
        fun stopMic() {
            runOnUiThread { shutdownMic() }
        }
    }

    private fun shutdownMic() {
        micOn = false
        handler.removeCallbacksAndMessages(null)
        recognizer?.destroy()
        recognizer = null
        muteBeep(false)
    }

    // suara "beep" recognizer dimatikan selama mic aktif (restart tiap beberapa detik)
    private fun muteBeep(mute: Boolean) {
        val dir = if (mute) AudioManager.ADJUST_MUTE else AudioManager.ADJUST_UNMUTE
        for (s in intArrayOf(AudioManager.STREAM_NOTIFICATION, AudioManager.STREAM_SYSTEM)) {
            try { audio?.adjustStreamVolume(s, dir, 0) } catch (e: Exception) { }
        }
    }

    private fun listen() {
        if (!micOn) return
        if (recognizer == null) {
            recognizer = SpeechRecognizer.createSpeechRecognizer(this).also {
                it.setRecognitionListener(listener)
            }
        }
        val i = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH).apply {
            putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
            putExtra(RecognizerIntent.EXTRA_LANGUAGE, "id-ID")
            putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, true)
        }
        try { recognizer?.startListening(i) } catch (e: Exception) { restartSoon(800) }
    }

    private fun restartSoon(ms: Long) {
        if (!micOn) return
        handler.postDelayed({ listen() }, ms)
    }

    private val listener = object : RecognitionListener {
        override fun onReadyForSpeech(params: Bundle?) {}
        override fun onBeginningOfSpeech() {}
        override fun onRmsChanged(rmsdB: Float) {
            js("onMicLevel(${rmsdB})")
        }
        override fun onBufferReceived(buffer: ByteArray?) {}
        override fun onEndOfSpeech() {}
        override fun onEvent(eventType: Int, params: Bundle?) {}

        override fun onPartialResults(partialResults: Bundle?) {
            val t = partialResults?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)?.firstOrNull() ?: return
            js("onSpeech(${JSONObject.quote(t)}, false)")
        }

        override fun onResults(results: Bundle?) {
            val t = results?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)?.firstOrNull()
            if (!t.isNullOrBlank()) js("onSpeech(${JSONObject.quote(t)}, true)")
            restartSoon(250)
        }

        override fun onError(error: Int) {
            when (error) {
                SpeechRecognizer.ERROR_INSUFFICIENT_PERMISSIONS -> {
                    shutdownMic()
                    js("onMicError('Izin mic ditolak')")
                }
                SpeechRecognizer.ERROR_RECOGNIZER_BUSY -> {
                    recognizer?.cancel()
                    restartSoon(900)
                }
                SpeechRecognizer.ERROR_CLIENT -> {
                    js("onMicInfo('Mic error kode $error')")
                    restartSoon(900)
                }
                else -> {
                    if (error != SpeechRecognizer.ERROR_NO_MATCH && error != SpeechRecognizer.ERROR_SPEECH_TIMEOUT) {
                        js("onMicInfo('Mic error kode $error')")
                    }
                    restartSoon(400)
                }
            }
        }
    }

    override fun onPause() {
        super.onPause()
        if (micOn) {
            shutdownMic()
            js("onMicError('Mic dimatikan (app di background)')")
        }
    }

    override fun onDestroy() {
        shutdownMic()
        web.destroy()
        super.onDestroy()
    }
}
