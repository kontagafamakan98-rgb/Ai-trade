package com.aitrade.ui

import androidx.compose.animation.*
import androidx.compose.animation.core.*
import androidx.compose.foundation.*
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.layout.*
import androidx.compose.material.icons.filled.*
import androidx.compose.material.icons.outlined.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.graphics.nativeCanvas
import androidx.compose.ui.input.pointer.pointerInput
import com.aitrade.engine.*
import com.aitrade.ui.theme.*
import java.util.*
import kotlin.math.roundToInt

@Composable
fun PortfolioEquityCanvas(
    points: List<EquityPoint>,
    startingBalance: Double,
    touchedIndex: Int?,
    onPointTouched: (Int?) -> Unit,
) {
    Canvas(
        modifier =
            Modifier
                .fillMaxSize()
                .pointerInput(points) {
                    detectTapGestures(
                        onTap = { offset ->
                            val w = size.width
                            val paddingLeft = 110f
                            val paddingRight = 30f
                            val usableWidth = w - paddingLeft - paddingRight
                            if (points.isNotEmpty()) {
                                val step = usableWidth / (points.size - 1).coerceAtLeast(1)
                                val rawIdx = ((offset.x - paddingLeft) / step).roundToInt()
                                val clampedIdx = rawIdx.coerceIn(0, points.size - 1)
                                onPointTouched(clampedIdx)
                            }
                        },
                    )
                },
    ) {
        if (points.isEmpty()) return@Canvas

        val w = size.width
        val h = size.height
        val paddingLeft = 110f // For Y axis labels
        val paddingRight = 30f
        val paddingTop = 30f
        val paddingBottom = 40f

        val usableWidth = w - paddingLeft - paddingRight
        val usableHeight = h - paddingTop - paddingBottom

        val minVal = (points.minOf { it.equity } * 0.995).coerceAtMost(startingBalance * 0.99)
        val maxVal = (points.maxOf { it.equity } * 1.005).coerceAtLeast(startingBalance * 1.01)
        val range = (maxVal - minVal).coerceAtLeast(100.0)

        // Draw Y-Axis Gridlines and Labels
        val gridCount = 4
        val textPaint =
            android.graphics.Paint().apply {
                color = android.graphics.Color.GRAY
                textSize = 26f
                typeface = android.graphics.Typeface.MONOSPACE
            }

        for (i in 0..gridCount) {
            val ratio = i.toFloat() / gridCount
            val y = paddingTop + usableHeight * (1f - ratio)
            val valAtY = minVal + range * ratio

            // Gridline
            drawLine(
                color = BorderColor.copy(alpha = 0.5f),
                start = Offset(paddingLeft, y),
                end = Offset(w - paddingRight, y),
                strokeWidth = 1f,
                pathEffect =
                    androidx.compose.ui.graphics.PathEffect
                        .dashPathEffect(floatArrayOf(8f, 8f), 0f),
            )

            // Y Label
            drawContext.canvas.nativeCanvas.drawText(
                "\$${String.format("%,.0f", valAtY)}",
                10f,
                y + 8f,
                textPaint,
            )
        }

        // Draw Starting Balance Baseline ($100,000)
        val baseRatio = ((startingBalance - minVal) / range).toFloat().coerceIn(0f, 1f)
        val baseY = paddingTop + usableHeight * (1f - baseRatio)
        drawLine(
            color = GoldYellow.copy(alpha = 0.7f),
            start = Offset(paddingLeft, baseY),
            end = Offset(w - paddingRight, baseY),
            strokeWidth = 2f,
            pathEffect =
                androidx.compose.ui.graphics.PathEffect
                    .dashPathEffect(floatArrayOf(12f, 6f), 0f),
        )

        // Compute Point Coordinates
        val coords =
            points.mapIndexed { idx, pt ->
                val xRatio = if (points.size > 1) idx.toFloat() / (points.size - 1) else 0.5f
                val yRatio = ((pt.equity - minVal) / range).toFloat().coerceIn(0f, 1f)
                val x = paddingLeft + usableWidth * xRatio
                val y = paddingTop + usableHeight * (1f - yRatio)
                Offset(x, y)
            }

        // Build Curve Path
        val path = Path()
        coords.forEachIndexed { i, pt ->
            if (i == 0) {
                path.moveTo(pt.x, pt.y)
            } else {
                val prev = coords[i - 1]
                val cx1 = (prev.x + pt.x) / 2f
                val cy1 = prev.y
                val cx2 = (prev.x + pt.x) / 2f
                val cy2 = pt.y
                path.cubicTo(cx1, cy1, cx2, cy2, pt.x, pt.y)
            }
        }

        // Build Gradient Fill Path
        val fillPath =
            Path().apply {
                addPath(path)
                lineTo(coords.last().x, paddingTop + usableHeight)
                lineTo(coords.first().x, paddingTop + usableHeight)
                close()
            }

        val overallProfitable = points.last().equity >= startingBalance
        val lineColor = if (overallProfitable) ProfitGreen else LossRed

        drawPath(
            path = fillPath,
            brush =
                Brush.verticalGradient(
                    colors = listOf(lineColor.copy(alpha = 0.35f), Color.Transparent),
                    startY = paddingTop,
                    endY = paddingTop + usableHeight,
                ),
        )

        drawPath(
            path = path,
            color = lineColor,
            style = Stroke(width = 5f, cap = StrokeCap.Round),
        )

        // Draw Data Nodes & End Point Glow
        coords.forEachIndexed { i, pt ->
            drawCircle(
                color = DarkCard,
                radius = 6f,
                center = pt,
            )
            drawCircle(
                color = lineColor,
                radius = 4f,
                center = pt,
            )
        }

        val lastPt = coords.last()
        drawCircle(color = lineColor.copy(alpha = 0.3f), radius = 12f, center = lastPt)
        drawCircle(color = lineColor, radius = 6f, center = lastPt)

        // Draw Touched/Selected Crosshair & Highlight
        touchedIndex?.let { tIdx ->
            if (tIdx in coords.indices) {
                val tPt = coords[tIdx]
                drawLine(
                    color = CyberBlue,
                    start = Offset(tPt.x, paddingTop),
                    end = Offset(tPt.x, paddingTop + usableHeight),
                    strokeWidth = 2f,
                    pathEffect =
                        androidx.compose.ui.graphics.PathEffect
                            .dashPathEffect(floatArrayOf(6f, 6f), 0f),
                )
                drawCircle(color = CyberBlue.copy(alpha = 0.4f), radius = 16f, center = tPt)
                drawCircle(color = CyberBlue, radius = 8f, center = tPt)
                drawCircle(color = Color.White, radius = 4f, center = tPt)
            }
        }
    }
}
