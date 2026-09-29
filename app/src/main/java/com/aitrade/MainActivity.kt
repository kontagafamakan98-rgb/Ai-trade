package com.aitrade

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.material3.Surface
import androidx.compose.ui.Modifier
import androidx.lifecycle.viewmodel.compose.viewModel
import com.aitrade.ui.TerminalDashboard
import com.aitrade.ui.TradingViewModel
import com.aitrade.ui.theme.AiTradeTheme

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        // Enable full-screen edge-to-edge UI drawing
        enableEdgeToEdge()

        setContent {
            AiTradeTheme {
                Surface(
                    modifier = Modifier.fillMaxSize(),
                    color = com.aitrade.ui.theme.CharcoalBackground,
                ) {
                    val viewModel: TradingViewModel = viewModel()
                    TerminalDashboard(viewModel = viewModel)
                }
            }
        }
    }
}
