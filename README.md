Running the bot in a docker continer guide: 
https://gemini.google.com/app/a33598f20082cceb


The Step-by-Step Execution Plan
Build and Start:
Open your terminal in the project folder and run:

Bash
docker-compose up -d --build
The One-Time Setup:

Download a VNC Viewer (like RealVNC).

Connect to localhost:5900.

You will see the MetaTrader 5 interface. Log in manually to your HFM account.

CRITICAL: Go to Tools -> Options -> Expert Advisors and check the box "Allow Algorithmic Trading".

Release the Bot:
In your host terminal (not the VNC), run:

Bash
docker exec -it zone_bot touch /app/ready_to_trade
Monitor the Logs:
Now you can watch the bot work in real-time:

Bash
docker logs -f zone_bot
Final Details
Image Size: The final image will be approximately 1.2GB to 1.5GB. This is because it includes a full Wine environment, the X11 window system, Python, and the MetaTrader 5 installation files.

Performance: Ensure your machine (or VPS) has at least 2GB of RAM available for this container. MT5 running via Wine is more RAM-intensive than a native Linux app.

