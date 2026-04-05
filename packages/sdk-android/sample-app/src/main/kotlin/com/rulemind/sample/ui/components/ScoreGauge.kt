package com.rulemind.sample.ui.components

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.layout.*
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.rulemind.sample.ui.theme.*

@Composable
fun ScoreGauge(score: Double, maxScore: Double, label: String) {
    val fraction = (score / maxScore).coerceIn(0.0, 1.0).toFloat()
    val arcColor = when {
        fraction >= 0.7 -> ApproveGreen
        fraction >= 0.5 -> ReviewAmber
        else -> RejectRed
    }

    Column(horizontalAlignment = Alignment.CenterHorizontally) {
        Box(contentAlignment = Alignment.Center, modifier = Modifier.size(140.dp)) {
            Canvas(modifier = Modifier.size(130.dp)) {
                val strokeWidth = 14.dp.toPx()
                val arcSize = Size(size.width - strokeWidth, size.height - strokeWidth)
                val topLeft = Offset(strokeWidth / 2, strokeWidth / 2)

                // Background arc
                drawArc(
                    color = Color(0xFFE2E8F0),
                    startAngle = 135f,
                    sweepAngle = 270f,
                    useCenter = false,
                    topLeft = topLeft,
                    size = arcSize,
                    style = Stroke(width = strokeWidth, cap = StrokeCap.Round),
                )
                // Score arc
                drawArc(
                    color = arcColor,
                    startAngle = 135f,
                    sweepAngle = 270f * fraction,
                    useCenter = false,
                    topLeft = topLeft,
                    size = arcSize,
                    style = Stroke(width = strokeWidth, cap = StrokeCap.Round),
                )
            }
            Column(horizontalAlignment = Alignment.CenterHorizontally) {
                Text(score.toInt().toString(), fontSize = 28.sp, fontWeight = FontWeight.Bold, color = arcColor)
                Text("/ ${maxScore.toInt()}", fontSize = 12.sp, color = Color.Gray)
            }
        }
        Text(label, fontSize = 13.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
    }
}
