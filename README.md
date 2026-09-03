# eBay AU + US Configuration-Based Pricing Engine

A production-ready Python application that determines the **cheapest real-world cost to obtain exact hardware configurations from eBay Australia and eBay United States**.

## Key Features

### 🎯 Configuration-Based Matching (NOT Part Number Scraping)

- **Product Description is the primary source of truth** - The system parses descriptions into structured configurations
- **Part Number is supporting identification only** - Used for validation, not as an exact search requirement
- **Semantic understanding** of hardware specifications:
  - Manufacturer, product family, model
  - CPU type, speed, quantity
  - RAM capacity and type
  - Drive configurations (distinguishes bay count from included drives)
  - Form factor, rack units, generation

### 🔍 Intelligent eBay Search

- Searches **eBay Australia (EBAY_AU)** and **eBay United States (EBAY_US)** only
- Uses eBay Browse API production environment
- Generates smart search queries from parsed configurations
- Caches repeated searches to minimize API calls

### 💰 Complete vs Component Pricing

The engine compares two acquisition strategies:

1. **Complete Listing**: Single eBay listing with full configuration
2. **Component Build**: Multiple listings combined to build the configuration
   - Base server/chassis
   - Individual CPUs (with bundle detection)
   - RAM modules
   - Compatible components

Returns the **cheapest valid option** with full price breakdown.

### 📦 Bundle Detection

- Recognizes multi-pack listings (e.g., "5 Pack Intel Xeon")
- Calculates actual acquisition cost based on required quantity
- Compares bundles vs individual purchases

### ⚠️ Smart Quantity Interpretation

Distinguishes between:
- `16x SFF` = 16 drive bays (NOT 16 drives)
- `5 Pack CPU` = quantity 5 CPUs
- `Dual CPU` = quantity 2 CPUs

### 🛡️ Rate Limit Handling

- Separate rate limit state for AU and US marketplaces
- Exponential backoff with jitter
- Retry-After header compliance
- Request caching
- **HTTP 429 never becomes NOT_FOUND**

### 💾 Checkpoint/Resume

- Saves progress after every N rows
- Resume from checkpoint if Colab disconnects
- Distinguishes RATE_LIMITED from NOT_FOUND status

### 📊 Output Format

Excel output includes:
- Original CSV columns preserved
- AU Item Price, Shipping, Total, Cheapest Price, Link
- US Item Price, Shipping, Total, Cheapest Price, Link
- Match Type (Complete Listing / Component Build / Bundle)
- Configuration/Component Summary
- Bundle Quantity
- Status (FOUND / NOT_FOUND / RATE_LIMITED / ERROR)
- Currency (AUD/USD)

**No seller information** is included (no usernames, ratings, feedback).

## Installation

### Google Colab (Recommended)

```python
# Install dependencies
!pip install pandas requests openpyxl tqdm

# Upload and run the script
from google.colab import files
uploaded = files.upload()  # Upload ebay_pricing_engine.py

# Run the engine
!python ebay_pricing_engine.py
```

### Local Installation

```bash
pip install -r requirements.txt
python ebay_pricing_engine.py
```

## Usage

1. **Upload CSV** with columns:
   - Required: `Part Number`, `Product Description`
   - Optional: `Brand`, `UOM`
   - All original columns are preserved

2. **Enter eBay API Credentials**:
   - Client ID (App ID)
   - Client Secret (Cert ID)
   - Must be Production credentials (not Sandbox)

3. **Processing**:
   - Each row is parsed into a structured configuration
   - eBay AU and US are searched independently
   - Results include complete listings and component builds
   - Progress saved to `ebay_pricing_checkpoint.csv`

4. **Download Results**:
   - Excel file (`ebay_pricing_results.xlsx`) automatically downloaded
   - Contains all pricing data and eBay links

## Example

### Input CSV Row

| Part Number | Product Description |
|-------------|---------------------|
| Dell1650-2xSL5XL | Dell PowerEdge 1650 Rackmount Server 2 x Intel Pentium III 1.4GHz CPU 2GB RAM |

### Parsed Configuration

```
Manufacturer: DELL
Product Family: PowerEdge
Model: 1650
Form Factor: rackmount
CPU: Intel Pentium III, 1.4GHz, Quantity: 2
RAM: 2GB
```

### Search Queries Generated

1. `DELL PowerEdge 1650`
2. `DELL PowerEdge 1650 Pentium III`
3. `DELL PowerEdge 1650 1.4GHz`
4. `DELL PowerEdge 1650 2GB RAM`
5. `PowerEdge 1650`

### Possible Results

**Option A - Complete Listing:**
```
Dell PowerEdge 1650 Dual Pentium III 1.4GHz 2GB RAM
Item: $150 + Shipping: $20 = Total: $170
```

**Option B - Component Build:**
```
Base Server: $50 + $20 = $70
CPU × 2: $15 + $5 each = $40
RAM 2GB: $10 + $5 = $15
Total: $125
```

**Final Result:** $125 (Component Build is cheaper)

## Architecture

```
┌─────────────────┐
│   CSV Upload    │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ Description     │
│ Parser          │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ Product         │
│ Configuration   │
└────────┬────────┘
         │
         ▼
┌─────────────────┐     ┌──────────────┐
│ Search Query    │────▶│ eBay API     │
│ Generator       │     │ (AU + US)    │
└─────────────────┘     └──────┬───────┘
                               │
                               ▼
┌─────────────────┐     ┌──────────────┐
│ Match Scoring   │◀────│ Raw Listings │
│ Engine          │     │              │
└────────┬────────┘     └──────────────┘
         │
         ▼
┌─────────────────┐
│ Component       │
│ Pricer          │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ Compare Complete│
│ vs Components   │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ Excel Export    │
└─────────────────┘
```

## Status Codes

| Status | Meaning |
|--------|---------|
| `FOUND` | Valid pricing found (complete or component) |
| `NOT_FOUND` | No valid listings found after all searches |
| `RATE_LIMITED` | API rate limit hit (will resume) |
| `ERROR` | Processing error occurred |
| `INCOMPLETE_CONFIGURATION` | Could not parse product description |

## Match Types

| Type | Description |
|------|-------------|
| `Complete Listing` | Single listing contains full configuration |
| `Component Build` | Multiple listings combined |
| `Bundle` | Multi-pack listing detected |

## API Requirements

- **eBay Production API** (not Sandbox)
- OAuth 2.0 Client Credentials grant
- Scope: `https://api.ebay.com/oauth/api_scope`
- Browse API access enabled

Get your credentials at: https://developer.ebay.com/

## Important Notes

1. **Production Only**: Uses live eBay API, not mock data
2. **No Hardcoded Credentials**: User enters credentials at runtime
3. **No Seller Data**: Output excludes seller information
4. **Price = Item + Shipping**: Always compares total acquisition cost
5. **Configuration > Part Number**: Description drives matching, P/N supports
6. **Checkpoint Safe**: Can resume after disconnection

## License

This software is provided as-is for legitimate eBay API usage.
