package com.aitrade.engine

import kotlin.random.Random

object MarketService {
    private val priceHistory = mutableMapOf<String, MutableList<Double>>()

    // Default starting prices
    private val basePrices =
        mapOf(
            "AAPL" to 180.0,
            "MSFT" to 420.0,
            "GOOGL" to 175.0,
            "BTC" to 65000.0,
            "ETH" to 3400.0,
            "TSLA" to 220.0,
            "NVDA" to 120.0,
        )

    init {
        // Initialize histories for each asset
        basePrices.forEach { (asset, basePrice) ->
            val list = mutableListOf<Double>()
            var current = basePrice
            // Create 50 historical candles
            for (i in 0 until 50) {
                val change = Random.nextDouble(-0.02, 0.022)
                current *= (1.0 + change)
                list.add(current)
            }
            priceHistory[asset] = list
        }
    }

    /**
     * Get the last closing prices list.
     */
    fun getCloses(asset: String): List<Double> {
        val cleanAsset = asset.uppercase().trim().replace("-USD", "")
        return priceHistory[cleanAsset] ?: run {
            // Generate a lazy default
            val defaultList = mutableListOf<Double>()
            var current = 100.0
            for (i in 0 until 50) {
                current *= (1.0 + Random.nextDouble(-0.015, 0.015))
                defaultList.add(current)
            }
            priceHistory[cleanAsset] = defaultList
            defaultList
        }
    }

    /**
     * Get the absolute latest price.
     */
    fun getLastPrice(asset: String): Double = getCloses(asset).last()

    /**
     * Simulated market tick.
     * Generates a new close price for each asset and appends it to history.
     */
    fun tickPrices() {
        priceHistory.forEach { (asset, history) ->
            val last = history.last()
            // Random walk between -0.8% and +0.95% (slightly positive drift)
            val change = Random.nextDouble(-0.008, 0.0095)
            val next = last * (1.0 + change)

            history.add(next)
            // Keep history limited to 100 entries
            if (history.size > 100) {
                history.removeAt(0)
            }
        }
    }

    /**
     * Generates random news headlines for specific assets to feed the AI context.
     */
    fun getNewsHeadlines(asset: String): String {
        val clean = asset.uppercase().trim()
        val randomNum = Random.nextInt(3)
        return when (clean) {
            "BTC", "ETH" -> {
                when (randomNum) {
                    0 ->
                        "Institutional inflows into spot ETFs hit a record high this week. Regulatory pressure on decentralized " +
                            "protocols seems to ease in several regions."
                    1 ->
                        "Whale movements indicate potential distribution phase. Concerns arise over network gas fees spike during " +
                            "sudden transaction volume peaks."
                    else ->
                        "Macro economic forecast remains highly positive. Traditional investors view crypto assets as the premier " +
                            "hedge against geopolitical inflation threats."
                }
            }
            "AAPL" -> {
                when (randomNum) {
                    0 ->
                        "Pre-orders for the next-gen AI-integrated devices blow past supply expectations. Supply chain components " +
                            "increase shipments early."
                    1 ->
                        "Antitrust investigation challenges the app store fee structure in key European hubs. Profit margins could see " +
                            "mild compression."
                    else -> "Apple expands services ecosystem into high-growth financial sectors. Analysts upgrade guidance to outperform."
                }
            }
            "TSLA" -> {
                when (randomNum) {
                    0 ->
                        "Delivery counts surprise Wall Street with a 5% beat on quarterly benchmarks. Energy storage business margins " +
                            "double."
                    1 -> "Recall on autopilot features raises regulatory scrutiny. Global production lines face temporary logistics shifts."
                    else -> "Tesla schedules massive robotaxi reveal Event. Pre-bookings drive retail interest to yearly highs."
                }
            }
            else -> {
                when (randomNum) {
                    0 -> "Record earnings beat expectations by 12%. Demand for cloud-based AI infrastructure reaches unprecedented levels."
                    1 -> "CEO announces strategic stock buyback program worth 10B. Integration of new AI copilot is met with warm reviews."
                    else -> "Global supply chains stabilize, allowing faster product distribution. Competition in the sector intensifies."
                }
            }
        }
    }
}
