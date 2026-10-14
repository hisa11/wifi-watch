package jp.robocon.wifiwatch

import jp.robocon.wifiwatch.core.Bodies
import jp.robocon.wifiwatch.net.RelayApi
import jp.robocon.wifiwatch.net.RelayConfigSource
import jp.robocon.wifiwatch.net.RelayException
import kotlinx.coroutines.runBlocking
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Before
import org.junit.Test

class RelayApiTest {
    private lateinit var server: MockWebServer
    private lateinit var api: RelayApi
    private val token = "app-token-0123456789abcdef"

    @Before
    fun setUp() {
        server = MockWebServer().apply { start() }
        val cfg = object : RelayConfigSource {
            override val relayUrl = server.url("/").toString().trimEnd('/')
            override val token = this@RelayApiTest.token
            override val pinCellular = false
            override val configured = true
        }
        api = RelayApi(cfg) { null }
    }

    @After
    fun tearDown() = server.shutdown()

    @Test
    fun sendsTokenPathsAndMethods() = runBlocking {
        repeat(4) { server.enqueue(MockResponse().setBody("""{"now":1,"state":null,"state_received_at":null,"state_age_ms":null}""")) }
        api.poll(true)
        val r1 = server.takeRequest()
        assertEquals("POST", r1.method)
        assertEquals("/api/app/poll", r1.path)
        assertEquals("Bearer $token", r1.getHeader("Authorization"))
        assertEquals(true, JSONObject(r1.body.readUtf8()).getBoolean("opened"))
        api.label("scan-1", Bodies.label("l1", "ピット", listOf("a"), "", 1.0))
        val r2 = server.takeRequest()
        assertEquals("PATCH", r2.method)
        assertEquals("/api/app/records/scan-1", r2.path)
        api.records(123)
        assertEquals("/api/app/records?since=123&limit=200", server.takeRequest().path)
        api.command(Bodies.scan("s1", listOf("6"), null, false))
        val r4 = server.takeRequest()
        assertEquals("/api/app/commands", r4.path)
        assertEquals("scan", JSONObject(r4.body.readUtf8()).getString("type"))
    }

    @Test
    fun mapsErrors() = runBlocking {
        server.enqueue(MockResponse().setResponseCode(401).setBody("""{"error":"unauthorized","message":"トークンがないか、違います"}"""))
        server.enqueue(MockResponse().setResponseCode(409).setBody("""{"error":"busy","message":"x"}"""))
        server.enqueue(MockResponse().setResponseCode(503).setBody("""{"error":"unavailable","message":"y"}"""))
        try { api.poll(false); fail() } catch (e: RelayException) { assertEquals(401, e.status); assertTrue(e.permanent) }
        try { api.command(Bodies.scan("s", null, null, false)); fail() } catch (e: RelayException) { assertEquals("busy", e.code) }
        try { api.poll(false); fail() } catch (e: RelayException) { assertEquals(503, e.status); assertTrue(!e.permanent) }
    }
}
