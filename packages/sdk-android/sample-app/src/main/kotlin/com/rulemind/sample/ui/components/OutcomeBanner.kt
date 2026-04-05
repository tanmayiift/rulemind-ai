package com.rulemind.sample.ui.components

import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.CheckCircle
import androidx.compose.material.icons.filled.Cancel
import androidx.compose.material.icons.filled.HourglassTop
import androidx.compose.material3.*
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.rulemind.sample.ui.theme.*

@Composable
fun OutcomeBanner(outcome: String, latencyMs: Any?) {
    val (color, icon, label) = when (outcome.lowercase()) {
        "approve" -> Triple(ApproveGreen, Icons.Filled.CheckCircle, "APPROVED")
        "reject" -> Triple(RejectRed, Icons.Filled.Cancel, "REJECTED")
        else -> Triple(ReviewAmber, Icons.Filled.HourglassTop, "REVIEW")
    }

    Card(
        modifier = Modifier.fillMaxWidth(),
        shape = RoundedCornerShape(16.dp),
        colors = CardDefaults.cardColors(containerColor = color),
    ) {
        Row(
            modifier = Modifier.padding(20.dp),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            Icon(icon, contentDescription = null, tint = Color.White, modifier = Modifier.size(36.dp))
            Column(modifier = Modifier.weight(1f)) {
                Text(label, color = Color.White, fontSize = 22.sp, fontWeight = FontWeight.Bold)
                if (latencyMs != null) {
                    Text("Evaluated in ${latencyMs}ms", color = Color.White.copy(alpha = 0.85f), fontSize = 13.sp)
                }
            }
        }
    }
}
