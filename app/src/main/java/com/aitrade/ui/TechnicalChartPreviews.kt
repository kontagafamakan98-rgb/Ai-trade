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
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.unit.dp
import com.aitrade.engine.*
import com.aitrade.ui.theme.*
import java.util.*

// --- TECHNICAL CHART CANVASES ---

@Composable
fun DoubleBottomChartPreview() {
    Canvas(modifier = Modifier.size(90.dp, 60.dp)) {
        val path =
            Path().apply {
                moveTo(10f, 10f)
                lineTo(30f, 50f) // First bottom low
                lineTo(45f, 25f) // Middle peak
                lineTo(60f, 50f) // Second bottom low
                lineTo(80f, 15f) // Breakout!
            }
        drawPath(
            path = path,
            color = CyberBlue,
            style = Stroke(width = 4f),
        )
        // Draw Neckline
        drawLine(
            color = Color.Gray,
            start =
                androidx.compose.ui.geometry
                    .Offset(15f, 25f),
            end =
                androidx.compose.ui.geometry
                    .Offset(75f, 25f),
            strokeWidth = 2f,
            pathEffect =
                androidx.compose.ui.graphics.PathEffect
                    .dashPathEffect(floatArrayOf(5f, 5f), 0f),
        )
    }
}

@Composable
fun HeadAndShouldersChartPreview() {
    Canvas(modifier = Modifier.size(90.dp, 60.dp)) {
        val path =
            Path().apply {
                moveTo(10f, 40f)
                lineTo(25f, 25f) // Left shoulder peak
                lineTo(35f, 35f) // Neck low 1
                lineTo(45f, 10f) // Head peak
                lineTo(55f, 35f) // Neck low 2
                lineTo(65f, 25f) // Right shoulder peak
                lineTo(80f, 50f) // Neckline breakdown!
            }
        drawPath(
            path = path,
            color = LossRed,
            style = Stroke(width = 4f),
        )
        // Draw Neckline
        drawLine(
            color = Color.Gray,
            start =
                androidx.compose.ui.geometry
                    .Offset(30f, 35f),
            end =
                androidx.compose.ui.geometry
                    .Offset(60f, 35f),
            strokeWidth = 2f,
            pathEffect =
                androidx.compose.ui.graphics.PathEffect
                    .dashPathEffect(floatArrayOf(5f, 5f), 0f),
        )
    }
}

@Composable
fun RsiDivergenceChartPreview() {
    Canvas(modifier = Modifier.size(90.dp, 60.dp)) {
        // Price falling
        val pricePath =
            Path().apply {
                moveTo(10f, 15f)
                lineTo(35f, 30f) // Peak low 1
                lineTo(50f, 20f)
                lineTo(75f, 35f) // Lower price low (divergent)
            }
        drawPath(
            path = pricePath,
            color = Color.White,
            style = Stroke(width = 3f),
        )

        // Indicator rising
        val rsiPath =
            Path().apply {
                moveTo(10f, 45f)
                lineTo(35f, 55f) // RSI low 1
                lineTo(50f, 50f)
                lineTo(75f, 48f) // Higher RSI low (divergence bullish!)
            }
        drawPath(
            path = rsiPath,
            color = CyberBlue,
            style = Stroke(width = 3f),
        )
    }
}
