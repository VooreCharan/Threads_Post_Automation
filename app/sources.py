"""Free public RSS feeds from Indian publishers, grouped by category. Add or remove feeds freely.

Only feeds whose article pages can be read are kept (NDTV, NDTV Sports and Gadgets 360 block automated
page reads and give only a one-line summary, so they were removed).

A feed entry is (name, url) or (name, url, "mixed"). "mixed" marks a feed that also carries foreign
stories: only items mentioning India or an Indian place/institution/company are kept from it.
Everything that survives is also checked by the AI writer, which rejects stories that are not mainly
about India (see INDIA_ONLY in config.py).
"""

FEEDS: dict[str, list[tuple[str, ...]]] = {
    "india": [
        ("The Hindu", "https://www.thehindu.com/news/national/feeder/default.rss"),
        ("Times of India", "https://timesofindia.indiatimes.com/rssfeeds/-2128936835.cms"),
        ("Indian Express", "https://indianexpress.com/section/india/feed/"),
        ("Hindustan Times", "https://www.hindustantimes.com/feeds/rss/india-news/rssfeed.xml"),
        ("Scroll", "https://feeds.feedburner.com/ScrollinArticles.rss"),
        ("Indian Express", "https://indianexpress.com/section/political-pulse/feed/"),
    ],
    "business": [
        ("The Hindu", "https://www.thehindu.com/business/feeder/default.rss"),
        ("Times of India", "https://timesofindia.indiatimes.com/rssfeeds/1898055.cms"),
        ("Indian Express", "https://indianexpress.com/section/business/feed/"),
        ("Hindustan Times", "https://www.hindustantimes.com/feeds/rss/business/rssfeed.xml"),
        ("Mint", "https://www.livemint.com/rss/news"),
        ("Economic Times", "https://economictimes.indiatimes.com/rssfeedstopstories.cms"),
    ],
    "technology": [
        ("The Hindu", "https://www.thehindu.com/sci-tech/technology/feeder/default.rss"),
        ("Indian Express", "https://indianexpress.com/section/technology/feed/"),
        ("Hindustan Times", "https://www.hindustantimes.com/feeds/rss/technology/rssfeed.xml"),
        ("Mint", "https://www.livemint.com/rss/technology"),
    ],
    "sports": [
        ("The Hindu", "https://www.thehindu.com/sport/feeder/default.rss"),
        ("Indian Express", "https://indianexpress.com/section/sports/feed/"),
        ("Hindustan Times", "https://www.hindustantimes.com/feeds/rss/cricket/rssfeed.xml"),
    ],
    "entertainment": [
        ("The Hindu", "https://www.thehindu.com/entertainment/feeder/default.rss"),
        ("Times of India", "https://timesofindia.indiatimes.com/rssfeeds/1081479906.cms"),
        ("Indian Express", "https://indianexpress.com/section/entertainment/feed/"),
        ("Hindustan Times", "https://www.hindustantimes.com/feeds/rss/entertainment/rssfeed.xml"),
    ],
    "health": [
        ("The Hindu", "https://www.thehindu.com/sci-tech/health/feeder/default.rss"),
        ("Indian Express", "https://indianexpress.com/section/lifestyle/health/feed/"),
        ("Hindustan Times", "https://www.hindustantimes.com/feeds/rss/lifestyle/health/rssfeed.xml"),
    ],
    "science": [
        ("The Hindu", "https://www.thehindu.com/sci-tech/science/feeder/default.rss"),
        ("Times of India", "https://timesofindia.indiatimes.com/rssfeeds/-2128672765.cms"),
    ],
    "environment": [
        ("The Hindu", "https://www.thehindu.com/sci-tech/energy-and-environment/feeder/default.rss"),
        ("Times of India", "https://timesofindia.indiatimes.com/rssfeeds/2647163.cms"),
    ],
}

# Words that mark a story as Indian (used to filter "mixed" feeds before any AI call)
INDIA_HINTS = (
    r"india\w*|bharat|hindustan|desi|delhi|mumbai|bengaluru|bangalore|chennai|kolkata|hyderabad|pune|"
    r"ahmedabad|jaipur|lucknow|kerala|tamil nadu|karnataka|maharashtra|gujarat|punjab|haryana|"
    r"uttar pradesh|madhya pradesh|bihar|rajasthan|telangana|andhra|odisha|assam|goa|kashmir|"
    r"modi|bjp|lok sabha|rajya sabha|supreme court of india|rupee\w*|rs\.?\s?\d|\u20b9|sensex|nifty|rbi|sebi|"
    r"isro|drdo|bcci|ipl|ranji|jio|airtel|vodafone idea|tata|reliance|infosys|wipro|hcl|mahindra|"
    r"flipkart|zomato|swiggy|paytm|ola|bharti|adani|ambani|upi|aadhaar"
)
