package com.rulemind.sample

import android.os.Bundle
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity
import com.rulemind.android.RuleMind
import com.rulemind.core.models.RuleMindConfig

class MainActivity : AppCompatActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val textView = TextView(this).apply {
            textSize = 16f
            setPadding(32, 64, 32, 64)
            text = "RuleMind sample booting..."
        }
        setContentView(textView)

        RuleMind.initialize(
            context = applicationContext,
            config = RuleMindConfig(
                baseUrl = "https://api.rulemind.local",
                apiKey = "rm_live_your_key_here",
            ),
        )
        textView.text = "RuleMind initialized. Call RuleMind.syncNow() and RuleMind.evaluate(...) to see decisions."
    }
}
