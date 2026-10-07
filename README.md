# 🎬 Signor Ent Bot - Telegram Movie Store with Paystack Payments

A complete Telegram bot + web dashboard for selling movies with M-Pesa STK Push, card payments, and subscription plans.

## Features

- **Auto-Delivery**: Movies sent automatically after payment confirmation
- **M-Pesa STK Push**: Direct phone prompt (supports 07xx, 01xx, 2547xx, 2541xx)
- **Card/Bank Payments**: Via Paystack checkout
- **Weekly Subscription**: 7 days unlimited access
- **Monthly Subscription**: 30 days unlimited access
- **Bulk Upload**: Add multiple movies via CSV or JSON
- **Per-Movie Pricing**: Set individual prices or use default
- **Subscription Pricing**: Admin-configurable from dashboard
- **Real-time Stats**: Track sales, revenue, M-Pesa vs Card breakdown

## Quick Setup

### 1. Create Telegram Bot
1. Message [@BotFather](https://t.me/botfather)
2. Create new bot → copy token
3. Set commands:
   ```
   start - Browse & get movies
   mystatus - Check subscription status
   checkpayment - Check pending payment
   stats - Admin statistics
   setprice - Set default movie price
   ```

### 2. MongoDB Atlas
1. Create free cluster at [mongodb.com](https://mongodb.com)
2. Get connection string
3. Whitelist IP: `0.0.0.0/0`

### 3. Paystack
1. Create account at [paystack.com](https://paystack.com)
2. Get test keys (switch to live for production)
3. Add webhook: `https://your-service.onrender.com/api/paystack/webhook`
4. Enable M-Pesa channel

### 4. Environment Variables

Copy `.env.example` to `.env` and fill:

```bash
BOT_TOKEN=your_bot_token_from_botfather
MONGO_URI=mongodb+srv://user:pass@cluster.mongodb.net/movie_bot
ADMIN_ID=your_telegram_numeric_user_id
CHANNEL_ID=-100your_private_channel_id
PAYSTACK_SECRET_KEY=sk_test_xxxxxxxx
PAYSTACK_PUBLIC_KEY=pk_test_xxxxxxxx
WEBHOOK_URL=https://your-service.onrender.com
PORT=10000
```

**Get your Telegram user ID:** Message [@userinfobot](https://t.me/userinfobot)

**Get channel ID:**
1. Forward a message from your channel to [@userinfobot](https://t.me/userinfobot)
2. Add `-100` prefix (e.g., `1234567890` → `-1001234567890`)

### 5. Deploy to Render

#### Option A: Native Python (Recommended)
1. Push code to GitHub
2. In Render → New + → Web Service
3. Connect GitHub repo
4. Set environment variables from `.env`
5. Deploy!

#### Option B: Using render.yaml (Blueprint)
1. Push code to GitHub with `render.yaml`
2. In Render → Blueprints → New Blueprint Instance
3. Connect repo
4. Fill secret env vars in dashboard
5. Deploy!

### 6. Configure Channel
1. Add your bot as **admin** to your private movie channel
2. Grant permissions: Post Messages, Edit Messages, Delete Messages
3. Post a movie → right-click → Copy Message Link
4. Paste in dashboard to generate shareable link

## User Flow

```
1. User clicks movie link (t.me/YourBot?start=TOKEN)
2. Bot checks: Active subscription? → Deliver instantly
3. No subscription? → Show price + payment options
4. User pays via M-Pesa STK or Card
5. Webhook confirms → Movie delivered AUTOMATICALLY
6. User gets "Enjoy your movie!" message
```

## Subscription Flow

```
1. User clicks "Get Subscription" or /start
2. Selects Weekly (50 KES) or Monthly (150 KES)
3. Pays via M-Pesa STK or Card
4. Webhook confirms → Subscription activated
5. User can now click ANY movie link for instant FREE delivery
6. Bot shows "Delivered with your subscription!"
```

## Admin Dashboard

Access at `https://your-service.onrender.com/`

### Features:
- **Stats Overview**: Total movies, sales, revenue, M-Pesa vs Card, active subscriptions
- **Add Single Movie**: Paste Telegram link, auto-parses channel ID & message ID
- **Bulk Upload**: CSV or JSON upload multiple movies at once
- **Subscription Pricing**: Set Weekly and Monthly prices from dashboard
- **Default Price**: Set fallback price for new movies
- **Movie List**: View all movies with copy/delete actions
- **Active Subscriptions**: Track current subscribers
- **Payment History**: See all transactions with payment method

### Bulk Upload CSV Format:
```csv
title,private_link,channel_id,message_id,price,category
Inception (2010),https://t.me/c/1234567890/100,,,10,movie
Breaking Bad S01E01,https://t.me/c/1234567890/101,,,15,series
Naruto Movie 1,,-1001234567890,102,10,anime
```

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Admin dashboard |
| `/api/movies` | GET | List all movies |
| `/api/movies` | POST | Create single movie |
| `/api/movies/bulk` | POST | Bulk upload JSON |
| `/api/movies/bulk/csv` | POST | Bulk upload CSV |
| `/api/movies/bulk/template` | GET | Download CSV template |
| `/api/movies/<token>` | DELETE | Delete movie |
| `/api/settings/price` | GET/POST | Default price |
| `/api/settings/subscription-prices` | GET/POST | Subscription prices |
| `/api/stats` | GET | Dashboard stats |
| `/api/subscriptions` | GET | Active subscriptions |
| `/api/payments` | GET | Payment history |
| `/api/paystack/webhook` | POST | Paystack webhook |
| `/webhook` | POST | Telegram webhook |

## Tech Stack

- **Backend**: Python 3.11, Flask, python-telegram-bot v20
- **Database**: MongoDB (PyMongo)
- **Payments**: Paystack API (M-Pesa + Card)
- **Frontend**: Vanilla HTML/CSS/JS (no framework needed)
- **Hosting**: Render (Web Service)

## Troubleshooting

### M-Pesa STK not working?
- Ensure Paystack M-Pesa channel is enabled
- Check webhook URL is correct in Paystack dashboard
- Verify phone format: 07xx, 01xx, 2547xx, or 2541xx

### Movie not delivering?
- Bot must be admin in channel with "Post Messages" permission
- Channel ID must include `-100` prefix
- Check Render logs for errors

### Webhook not receiving?
- Ensure `WEBHOOK_URL` matches your Render service URL
- Check Paystack webhook signature verification
- Verify `/webhook` endpoint returns 200 OK

### Free Render tier sleeping?
- First request after 15 min inactivity may take 30s
- Consider upgrading to paid tier for 24/7 uptime
- Or use a cron job to ping `/health` every 10 minutes

## License

MIT - Use freely for your movie store!

## Support

For issues, check Render logs and Paystack dashboard for webhook events.
