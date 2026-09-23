package com.arbnor.xauai

import android.graphics.Color
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.os.Bundle
import android.Manifest
import android.content.pm.PackageManager
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import androidx.work.Constraints
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.NetworkType
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import java.util.concurrent.TimeUnit
import android.os.Handler
import android.os.Looper
import android.view.Gravity
import android.view.View
import android.widget.*
import androidx.appcompat.app.AppCompatActivity
import androidx.core.view.ViewCompat
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.doOnAttach
import org.json.JSONArray
import org.json.JSONObject
import java.time.Instant
import java.util.concurrent.Executors
import java.util.concurrent.Future

/** Native phone presentation. No broker or provider execution. */
class MainActivity : AppCompatActivity() {
    private val gold = Color.rgb(215,181,109)
    private val muted = Color.rgb(170,184,204)
    private val white = Color.rgb(242,244,249)
    private val handler = Handler(Looper.getMainLooper())
    private val executor = Executors.newSingleThreadExecutor()
    private val api = ApiClient()
    private var task: Future<*>? = null
    private var data: Dashboard? = null
    private var connected = false
    private var fetching = false
    private var generation = 0
    private var active = false
    private var message = "Loading backend information…"
    private var page = 0
    private var cohort = "CHAMPION"
    private var historyLimit = 30
    private lateinit var content: LinearLayout
    private lateinit var scroll: ScrollView
    private lateinit var navigation: LinearLayout
    private val prefs by lazy { getSharedPreferences("xauai",MODE_PRIVATE) }
    private var endpoint = ApiClient.DEFAULT_ENDPOINT
    private var endpointDraft: String? = null
    private var editingEndpoint = false
    private var lastRequest = 0L
    private val pulse = object : Runnable {
        override fun run() {
            if (!active) return
            if (System.currentTimeMillis()-lastRequest >= 60000 && !fetching && page != 3) sync()
            else if (page == 0) render(true)
            handler.postDelayed(this,15000)
        }
    }
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        // Own insets consistently, including Android 15's enforced edge-to-edge layout.
        WindowCompat.setDecorFitsSystemWindows(window, false)
        page=savedInstanceState?.getInt("page",0) ?: 0
        data=lastCustomNonConfigurationInstance as? Dashboard
        endpoint=prefs.getString("endpoint",ApiClient.DEFAULT_ENDPOINT) ?: ApiClient.DEFAULT_ENDPOINT
        setContentView(R.layout.activity_main)
        content=findViewById(R.id.content); scroll=findViewById(R.id.scroll); navigation=findViewById(R.id.navigation)
        val root=findViewById<View>(R.id.root)
        ViewCompat.setOnApplyWindowInsetsListener(root) { view, insets ->
            val safe=insets.getInsets(WindowInsetsCompat.Type.systemBars() or WindowInsetsCompat.Type.displayCutout())
            val keyboard=insets.getInsets(WindowInsetsCompat.Type.ime())
            // Absolute padding: repeated dispatch must not accumulate spacing.
            view.setPadding(safe.left,safe.top,safe.right,maxOf(safe.bottom,keyboard.bottom))
            WindowInsetsCompat.CONSUMED
        }
        root.doOnAttach { ViewCompat.requestApplyInsets(it) }
        render()
        enableSignalNotifications()
    }

    private fun enableSignalNotifications() {
        if (android.os.Build.VERSION.SDK_INT >= 33 &&
            ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            ActivityCompat.requestPermissions(this, arrayOf(Manifest.permission.POST_NOTIFICATIONS), 2001)
        }
        val constraints = Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build()
        val work = PeriodicWorkRequestBuilder<SignalSyncWorker>(15, TimeUnit.MINUTES)
            .setConstraints(constraints)
            .build()
        WorkManager.getInstance(this).enqueueUniquePeriodicWork(
            SignalSyncWorker.UNIQUE_WORK,
            ExistingPeriodicWorkPolicy.UPDATE,
            work
        )
    }
    override fun onStart() { super.onStart(); active=true; sync(); handler.postDelayed(pulse,15000) }
    override fun onStop() {
        active=false; generation++; fetching=false; task?.cancel(true); api.cancel()
        handler.removeCallbacksAndMessages(null); super.onStop()
    }
    override fun onDestroy() { executor.shutdownNow(); super.onDestroy() }
    override fun onSaveInstanceState(out: Bundle) { out.putInt("page",page); super.onSaveInstanceState(out) }
    override fun onRetainCustomNonConfigurationInstance(): Any? = data
    private fun sync() {
        if (fetching || !active) return
        fetching=true; lastRequest=System.currentTimeMillis(); val request=++generation
        val address=endpoint
        message="Syncing backend…"; render(true)
        task=executor.submit {
            var result: Dashboard?=null; var failure: String?=null
            try { result=api.fetch(address) } catch(e: ApiFailure) { failure=e.category }
            catch(_: Exception) { failure="Backend unavailable" }
            val captured=result; val error=failure
            handler.post {
                if(request!=generation || !active || isDestroyed) return@post
                fetching=false; connected=captured!=null
                if(captured!=null) { data=captured; message="Connected · DEMO / research" }
                else message=error ?: "Backend unavailable"
                if (!(page==3 && editingEndpoint)) render(true)
            }
        }
    }
    private fun dp(v: Int)=(v*resources.displayMetrics.density).toInt()
    private fun text(value: String,size: Float=16f,color: Int=white,bold: Boolean=false)=TextView(this).apply {
        text=value; textSize=size; setTextColor(color); setLineSpacing(dp(3).toFloat(),1f)
        typeface=Typeface.create("sans-serif",if(bold) Typeface.BOLD else Typeface.NORMAL)
        setPadding(0,dp(4),0,dp(4)); setTextIsSelectable(true)
    }
    private fun card(title: String,subtitle: String?=null,build: LinearLayout.()->Unit) {
        val box=LinearLayout(this).apply {
            orientation=LinearLayout.VERTICAL; setPadding(dp(18),dp(16),dp(18),dp(16))
            background=GradientDrawable().apply { setColor(Color.rgb(17,26,40)); cornerRadius=dp(20).toFloat(); setStroke(dp(1),Color.rgb(39,51,68)) }
            layoutParams=LinearLayout.LayoutParams(-1,-2).apply { bottomMargin=dp(14) }
            addView(text(title,18f,gold,true)); if(subtitle!=null) addView(text(subtitle,13f,muted))
        }
        box.build(); content.addView(box)
    }
    private fun LinearLayout.rows(rows: List<DisplayRow>) {
        rows.filter { it.value.isNotBlank() && !it.value.equals(Presentation.MISSING,true) && !it.value.equals("null",true) }.forEach { addView(text(it.label.uppercase(),12f,muted,true)); addView(text(it.value)) }
    }
    private fun LinearLayout.action(label: String,click: ()->Unit) {
        addView(Button(this@MainActivity).apply {
            text=label; isAllCaps=false; minHeight=dp(48); setTextColor(Color.rgb(8,13,22)); textSize=15f
            background=android.graphics.drawable.RippleDrawable(android.content.res.ColorStateList.valueOf(Color.argb(35,255,255,255)),GradientDrawable().apply { setColor(gold); cornerRadius=dp(12).toFloat() },null)
            setPadding(dp(12),dp(8),dp(12),dp(8)); setOnClickListener { click() }
        },LinearLayout.LayoutParams(-1,-2).apply { topMargin=dp(10) })
    }
    private fun render(keepScroll: Boolean=false) {
        val y=if(keepScroll) scroll.scrollY else 0
        content.removeAllViews(); navigation.removeAllViews()
        content.addView(text("DardaniaXAUTRADE AI",25f,white,true))
        content.addView(text("XAU/USD · Adaptive Intelligence",14f,gold))
        if(page==0) content.addView(text("Powered by ArbnorSylaj",13f,muted))
        content.addView(text("DEMO / RESEARCH",13f,gold,true))
        content.addView(text(message,14f,muted))
        if(fetching) content.addView(ProgressBar(this,null,android.R.attr.progressBarStyleHorizontal).apply { isIndeterminate=true },LinearLayout.LayoutParams(-1,dp(3)))
        val snapshot=data
        if(snapshot==null && page!=3) card(if(fetching) "Connecting" else "Backend unavailable","No trading data is being substituted.") {
            addView(text("A decision appears only after the backend returns it. Readiness is NOT_READY until verified.")); action("Retry sync") { sync() }
        } else when(page) { 0->home(snapshot!!); 1->signals(snapshot!!); 2->analytics(snapshot!!); else->settings() }
        content.addView(text("Research / DEMO mode. No guaranteed profit.",13f,muted))
        listOf("Home","Signals","Analytics","Settings").forEachIndexed { index,label ->
            navigation.addView(TextView(this).apply {
                text=label; textSize=12f; gravity=Gravity.CENTER; setTextColor(if(page==index) gold else muted)
                minHeight=dp(56); isSelected=page==index; isClickable=true; isFocusable=true
                contentDescription="$label${if(page==index) ", selected" else ""}"
                setOnClickListener { editingEndpoint=false; page=index; render() }
            },LinearLayout.LayoutParams(0,-1,1f))
        }
        scroll.post { if(!isDestroyed) scroll.scrollTo(0,y) }
    }
    private fun home(d: Dashboard) {
        val root=d.signal; val f=Presentation.champion(root); val now=Instant.now()
        card("CHAMPION · backend decision","Research presentation · no broker execution") {
            val direction=Presentation.direction(root)
            addView(text(Presentation.readable(direction),34f,when(direction) { "BUY"->Color.rgb(118,213,169); "SELL"->Color.rgb(245,151,153); else->gold },true))
            rows(listOf(DisplayRow("Confidence",Presentation.confidence(root)),DisplayRow("XAU/USD observed price",Presentation.number(f.number("monitor_price","current_price","price"))),
                DisplayRow("Data mode",Presentation.dataMode(root,now,connected)),DisplayRow("Backend readiness",Presentation.readiness(root)),
                DisplayRow("Decision timestamp",Presentation.time(f.text("timestamp_utc","timestamp"))),
                DisplayRow("Freshness",if(Presentation.stale(root,now)) "Stale, expired or timestamp unavailable" else "Decision age: ${now.epochSecond-Presentation.instant(f.text("timestamp_utc","timestamp"))!!.epochSecond} seconds${if(!connected) " · cached" else ""}")))
            action("Refresh") { sync() }
        }
        card("Trade Blueprint") { rows(Presentation.blueprint(root,now,connected)) }
        card("Intelligence Council","Backend indices, not guaranteed probabilities") {
            val advanced=Presentation.council(root)
            if(advanced.isEmpty()) addView(text("Advanced intelligence data is not available from the current backend.",14f,muted))
            rows(advanced)
            val existingLabels=advanced.map { it.label }.toSet()
            rows(Presentation.legacyMetrics(root).filter { it.label !in existingLabels })
        }
        Presentation.market(root).takeIf { it.isNotEmpty() }?.let { market -> card("Market State") { rows(market) } }
        card("Hidden-State Intelligence") {
            val hidden=details(f.obj("hidden_state","dgfe"))
            if(hidden.isEmpty()) addView(text("Hidden-state intelligence unavailable from current backend.",14f,muted)) else rows(hidden)
        }
        card("AI Explanation") {
            rows(listOf(DisplayRow("Reasons",f.list("reasons","explanations").joinToString("\n").ifBlank { Presentation.MISSING }),
                DisplayRow("Supporting factors",f.list("supporting_factors").joinToString("\n").ifBlank { Presentation.MISSING }),
                DisplayRow("Opposing factors",f.list("opposing_factors").joinToString("\n").ifBlank { Presentation.MISSING }),
                DisplayRow("Vetoes",f.list("veto_codes","vetoes").joinToString("\n").ifBlank { "None supplied by backend" }),
                DisplayRow("Data-quality warnings",(d.warnings+f.list("health.data_errors","freshness_errors")).joinToString("\n").ifBlank { "No additional warnings supplied" })).filter { it.value != Presentation.MISSING })
        }
        card("Macro & News","Source-reported context · supplied only through the backend") {
            val macro=details(f.obj("intelligence","macro_news"))
            if(macro.isEmpty()) addView(text("Macro & News intelligence unavailable until the forward-data backend is enabled.",14f,muted)) else rows(macro)
            Fields(root).obj("forex_factory","secondary_calendar")?.let { addView(text("Forex Factory · secondary cross-check only",15f,gold)); rows(details(it)) }
        }
        status(root)
        Fields(root).obj("challenger","meta.challenger")?.let { challenger ->
            card("CHALLENGER · isolated research","Does not replace the Champion or enter its performance totals") {
                rows(listOf(DisplayRow("Research decision",Fields(challenger).text("direction") ?: Presentation.MISSING),DisplayRow("Promotion","No automatic promotion")))
            }
        }
    }
    private fun status(root: JSONObject) {
        card("Provider / System Status","Backend report at last sync · check freshness above") {
            rows(listOf(DisplayRow("Backend connectivity",if(connected) "Connected" else "Not connected / cached data only"),
                DisplayRow("Readiness",Presentation.readiness(root)),DisplayRow("Data status",Presentation.dataMode(root,Instant.now(),connected))))
            rows(details(Fields(root).obj("provider_health","readiness.health","system.health","health")))
            val providers=Fields(root).value("providers") ?: Fields(root).value("system.providers")
            if(providers is JSONArray) rows(details(JSONObject().put("Provider candidates",providers)))
        }
    }
    private fun signals(d: Dashboard) {
        card("Signal History","Recorded decisions · historical data is never a current LIVE badge") { if(d.history.isEmpty()) addView(text("History is unavailable or empty. No sample trades have been added.")) }
        d.history.take(historyLimit).forEach { item ->
            val f=Fields(item)
            card("${Presentation.direction(item).replace('_',' ')} · ${Presentation.historyRole(item)}",Presentation.time(f.text("timestamp_utc","created_at","timestamp"))) {
                rows(listOf(DisplayRow("Confidence",Presentation.confidence(item)),DisplayRow("Regime",f.text("regime","mode","analytics_context.hidden_state") ?: Presentation.MISSING),
                    DisplayRow("Data mode at record",Presentation.historyDataMode(item))))
                if(Presentation.direction(item)!="NO_TRADE") rows(listOf("Entry" to "entry","Stop Loss" to "sl","TP1" to "tp1","TP2" to "tp2").map { DisplayRow(it.first,Presentation.number(f.number(it.second))) })
                rows(listOf(DisplayRow("DEMO outcome",f.text("result","outcome","status") ?: Presentation.MISSING),DisplayRow("Closed at",Presentation.time(f.text("closed_at"))),DisplayRow("Gross R",Presentation.number(f.number("gross_r","r_multiple"))),DisplayRow("Simulated net R",Presentation.number(f.number("simulated_net_r","net_r")))))
            }
        }
        if(historyLimit<d.history.size) card("More history") { action("Show next 30") { historyLimit+=30; render(true) } }
    }
    private fun analytics(d: Dashboard) {
        card("DEMO Analytics","Historical results are not a promise of future performance") {
            action(if(cohort=="CHAMPION") "Champion · switch to Challenger" else "Challenger research · switch to Champion") { cohort=if(cohort=="CHAMPION") "ADAPTIVE_CHALLENGER" else "CHAMPION"; render() }
            rows(Presentation.analytics(d.performance,cohort))
        }
        val f=Fields(d.performance ?: JSONObject())
        val evidence=listOf(DisplayRow("Real evaluation cycles",Presentation.number(f.number("real_forward_evaluation_cycles"),0)),DisplayRow("Real five-minute decisions",Presentation.number(f.number("real_five_minute_decisions"),0))).filter { it.value != Presentation.MISSING }
        if(evidence.isNotEmpty()) card("Forward evidence") { rows(evidence) }
        val qualified=Presentation.analytics(d.performance,cohort).none { it.value=="INSUFFICIENT_FORWARD_DATA" }
        val breakdown=details(f.obj("cohorts.$cohort.by_regime"))+details(f.obj("cohorts.$cohort.by_session"))
        if(qualified && breakdown.isNotEmpty()) card("Regime / Session Breakdown",cohort.replace('_',' ')) { rows(breakdown) }
    }
    private fun settings() {
        card("About","Powered by ArbnorSylaj") {
            @Suppress("DEPRECATION") val version=packageManager.getPackageInfo(packageName,0).versionName ?: Presentation.MISSING
            rows(listOf(DisplayRow("App","DardaniaXAUTRADE AI"),DisplayRow("Version",version),DisplayRow("Mode","Research / DEMO mode. No guaranteed profit."),
                DisplayRow("Connectivity",if(connected) "Connected" else "Not connected / cached data only"),DisplayRow("Readiness",data?.let { Presentation.readiness(it.signal) } ?: "NOT_READY"),
                DisplayRow("Last successful sync",data?.let { Presentation.time(it.syncedAt.toString()) } ?: "Never synced"),DisplayRow("Data mode",data?.let { Presentation.dataMode(it.signal,Instant.now(),connected) } ?: "UNAVAILABLE")))
        }
        card("Backend Connection","HTTPS only · no provider credentials belong in Android") {
            val input=EditText(this@MainActivity).apply {
                setText(endpointDraft ?: endpoint); textSize=15f; setTextColor(white); setHintTextColor(muted)
                inputType=android.text.InputType.TYPE_CLASS_TEXT or android.text.InputType.TYPE_TEXT_VARIATION_URI
                contentDescription="HTTPS signal endpoint"; minHeight=dp(56); maxLines=4
                addTextChangedListener(object : android.text.TextWatcher {
                    override fun beforeTextChanged(s: CharSequence?,start: Int,count: Int,after: Int) {}
                    override fun onTextChanged(s: CharSequence?,start: Int,before: Int,count: Int) { endpointDraft=s?.toString(); editingEndpoint=true }
                    override fun afterTextChanged(s: android.text.Editable?) {}
                })
            }
            addView(input,LinearLayout.LayoutParams(-1,-2))
            action("Save endpoint & sync") {
                try {
                    val candidate=ApiClient.signalEndpoint(input.text.toString())
                    endpoint=candidate; prefs.edit().putString("endpoint",endpoint).apply()
                    endpointDraft=null; editingEndpoint=false
                    generation++; task?.cancel(true); api.cancel(); fetching=false; data=null; connected=false; sync()
                } catch(_: Exception) { input.error="Enter HTTPS without credentials, query parameters or fragment" }
            }
        }
        data?.let { status(it.signal) }
    }
    private fun details(value: JSONObject?): List<DisplayRow> {
        if(value==null) return emptyList()
        val rows=mutableListOf<DisplayRow>()
        fun walk(v: Any?,path: String,depth: Int) {
            if(depth>5 || rows.size>=80) return
            when(v) {
                is JSONObject -> v.keys().asSequence().toList().sorted().forEach { key -> if(!Regex("(?i).*(token|secret|password|api.?key|authorization|credential).*").matches(key)) walk(v.opt(key),if(path.isEmpty()) key else "$path / $key",depth+1) }
                is JSONArray -> for(i in 0 until minOf(v.length(),12)) walk(v.opt(i),"$path ${i+1}",depth+1)
                is Number -> if(v.toDouble().isFinite()) rows.add(DisplayRow(Presentation.readable(path),Presentation.number(v.toDouble())))
                is String -> if(v.isNotBlank() && !v.equals(Presentation.MISSING,true) && v != "null") rows.add(DisplayRow(Presentation.readable(path),when {
                    v in setOf("LIVE","LIVE_DATA") -> "Backend mode reported; verify dashboard provenance"
                    v.contains("forexfactory",true) || v.contains("forex factory",true) -> "${v.take(1200)} · secondary cross-check only"
                    else -> v.take(1200)
                }))
                is Boolean -> rows.add(DisplayRow(Presentation.readable(path),if(v) "Yes" else "No"))
            }
        }
        walk(value,"",0); return rows
    }
}
