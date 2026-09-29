package com.aitrade.ui

import androidx.compose.animation.*
import androidx.compose.animation.core.*
import androidx.compose.foundation.*
import androidx.compose.foundation.layout.*
import androidx.compose.material.icons.filled.*
import androidx.compose.material.icons.outlined.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.unit.dp
import com.aitrade.engine.*
import com.aitrade.ui.theme.*
import java.util.*

@Composable
fun PriceChartCanvas(closes: List<Double>) {
    Canvas(
        modifier =
            Modifier
                .fillMaxWidth()
                .height(180.dp)
                .background(CharcoalBackground),
    ) {
        if (closes.size < 2) return@Canvas

        val maxPrice = closes.maxOrNull() ?: 1.0
        val minPrice = closes.minOrNull() ?: 0.0
        val range = if (maxPrice - minPrice == 0.0) 1.0 else maxPrice - minPrice

        val width = size.width
        val height = size.height

        val stepX = width / (closes.size - 1)

        // Draw grid lines
        val gridLines = 4
        for (i in 0..gridLines) {
            val y = (height / gridLines) * i
            drawLine(
                color = BorderColor.copy(alpha = 0.5f),
                start = Offset(0f, y),
                end = Offset(width, y),
                strokeWidth = 1f,
            )
        }

        // Helper to map double price to pixel Y coord
        fun getY(price: Double): Float = (height - ((price - minPrice) / range * height)).toFloat()

        // Draw price path
        val pricePath = Path()
        pricePath.moveTo(0f, getY(closes.first()))
        for (i in 1 until closes.size) {
            pricePath.lineTo(i * stepX, getY(closes[i]))
        }
        drawPath(
            path = pricePath,
            color = CyberBlue,
            style = Stroke(width = 3f),
        )

        // Draw EMA20 path
        val ema20List = DecisionEngine.calculateEma(closes, 20)
        if (ema20List.isNotEmpty()) {
            val ema20Path = Path()
            val offset = closes.size - ema20List.size
            ema20Path.moveTo((offset * stepX), getY(ema20List.first()))
            for (i in 1 until ema20List.size) {
                ema20Path.lineTo((offset + i) * stepX, getY(ema20List[i]))
            }
            drawPath(
                path = ema20Path,
                color = ProfitGreen,
                style = Stroke(width = 2f),
            )
        }

        // Draw EMA50 path
        val ema50List = DecisionEngine.calculateEma(closes, 50)
        if (ema50List.isNotEmpty()) {
            val ema50Path = Path()
            val offset = closes.size - ema50List.size
            ema50Path.moveTo((offset * stepX), getY(ema50List.first()))
            for (i in 1 until ema50List.size) {
                ema50Path.lineTo((offset + i) * stepX, getY(ema50List[i]))
            }
            drawPath(
                path = ema50Path,
                color = LossRed,
                style = Stroke(width = 2f),
            )
        }
    }
}
