"""
eBay AU + US Configuration-Based Pricing Engine
Production-ready application for Google Colab

This application:
1. Accepts CSV upload with Part Number and Product Description
2. Parses product descriptions into structured configurations
3. Searches eBay AU and US using configuration attributes (not exact part numbers)
4. Finds cheapest complete listings OR builds from compatible components
5. Calculates total acquisition cost (item price + shipping)
6. Exports results to Excel
"""

import pandas as pd
import requests
import re
import json
import time
import random
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, field, asdict
from enum import Enum
from collections import defaultdict
import os

# Colab-specific imports
try:
    from google.colab import files
    COLAB_AVAILABLE = True
except ImportError:
    COLAB_AVAILABLE = False


class MatchType(Enum):
    COMPLETE_LISTING = "Complete Listing"
    COMPONENT_BUILD = "Component Build"
    BUNDLE = "Bundle"
    PARTIAL_MATCH = "Partial Match"
    NOT_FOUND = "Not Found"
    RATE_LIMITED = "Rate Limited"
    ERROR = "Error"
    INCOMPLETE_CONFIGURATION = "Incomplete Configuration"


class Status(Enum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    RATE_LIMITED = "RATE_LIMITED"
    ERROR = "ERROR"
    INCOMPLETE_CONFIGURATION = "INCOMPLETE_CONFIGURATION"


@dataclass
class CPURequirement:
    manufacturer: str = ""
    model: str = ""
    speed_ghz: Optional[float] = None
    quantity: int = 1
    socket: str = ""
    generation: str = ""


@dataclass
class RAMRequirement:
    capacity_gb: Optional[float] = None
    quantity: int = 1
    ram_type: str = ""  # DDR, DDR2, DDR3, DDR4, etc.
    form_factor: str = ""  # DIMM, SODIMM, etc.


@dataclass
class DriveRequirement:
    drive_type: str = ""  # HDD, SSD, NVMe, etc.
    capacity_gb: Optional[float] = None
    quantity: int = 0
    form_factor: str = ""  # SFF, LFF, 2.5", 3.5", etc.
    interface: str = ""  # SATA, SAS, etc.


@dataclass
class PSURequirement:
    wattage: Optional[int] = None
    quantity: int = 0
    redundant: bool = False


@dataclass
class NetworkRequirement:
    speed: str = ""  # 1GbE, 10GbE, etc.
    ports: int = 0
    type: str = ""  # NIC, adapter, etc.


@dataclass
class ProductConfiguration:
    manufacturer: str = ""
    product_family: str = ""
    model: str = ""
    generation: str = ""
    form_factor: str = ""  # Rackmount, Tower, Desktop, etc.
    rack_units: Optional[int] = None
    cpu: CPURequirement = field(default_factory=CPURequirement)
    ram: RAMRequirement = field(default_factory=RAMRequirement)
    drives: List[DriveRequirement] = field(default_factory=list)
    psu: PSURequirement = field(default_factory=PSURequirement)
    network: NetworkRequirement = field(default_factory=NetworkRequirement)
    raid_controller: str = ""
    original_part_number: str = ""
    raw_description: str = ""
    additional_requirements: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict:
        return {
            'manufacturer': self.manufacturer,
            'product_family': self.product_family,
            'model': self.model,
            'generation': self.generation,
            'form_factor': self.form_factor,
            'rack_units': self.rack_units,
            'cpu_manufacturer': self.cpu.manufacturer,
            'cpu_model': self.cpu.model,
            'cpu_speed_ghz': self.cpu.speed_ghz,
            'cpu_quantity': self.cpu.quantity,
            'ram_capacity_gb': self.ram.capacity_gb,
            'ram_quantity': self.ram.quantity,
            'ram_type': self.ram.ram_type,
            'drive_count': len(self.drives),
            'psu_wattage': self.psu.wattage,
            'network_speed': self.network.speed,
            'original_part_number': self.original_part_number
        }


@dataclass
class eBayListing:
    item_id: str
    title: str
    item_price: float
    shipping_cost: float
    total_price: float
    currency: str
    url: str
    condition: str
    bundle_quantity: int = 1
    match_score: float = 0.0
    matched_attributes: List[str] = field(default_factory=list)
    configuration_summary: str = ""

    def to_dict(self) -> Dict:
        return {
            'item_id': self.item_id,
            'title': self.title,
            'item_price': self.item_price,
            'shipping_cost': self.shipping_cost,
            'total_price': self.total_price,
            'currency': self.currency,
            'url': self.url,
            'condition': self.condition,
            'bundle_quantity': self.bundle_quantity,
            'match_score': self.match_score,
            'matched_attributes': self.matched_attributes,
            'configuration_summary': self.configuration_summary
        }


@dataclass
class SearchResult:
    status: Status
    match_type: MatchType
    cheapest_price: Optional[float] = None
    link: str = ""
    item_price: Optional[float] = None
    shipping_cost: Optional[float] = None
    total_price: Optional[float] = None
    currency: str = ""
    configuration_summary: str = ""
    bundle_quantity: int = 1
    listings: List[eBayListing] = field(default_factory=list)
    error_message: str = ""

    def to_output_dict(self, prefix: str = "") -> Dict:
        prefix = prefix.upper() if prefix else ""
        if prefix:
            prefix = f"{prefix}_"
        
        return {
            f'{prefix}Item Price': self.item_price,
            f'{prefix}Shipping': self.shipping_cost,
            f'{prefix}Total': self.total_price,
            f'{prefix}Cheapest Price': self.cheapest_price,
            f'{prefix}Link': self.link,
            f'{prefix}Currency': self.currency,
            f'{prefix}Match Type': self.match_type.value if self.match_type else "",
            f'{prefix}Configuration/Component Summary': self.configuration_summary,
            f'{prefix}Bundle Quantity': self.bundle_quantity,
            f'{prefix}Status': self.status.value if self.status else ""
        }


class ProductDescriptionParser:
    """Parses product descriptions into structured configurations"""
    
    # Manufacturer mappings
    MANUFACTURERS = {
        'dell': ['dell', 'poweredge', 'optiplex', 'precision', 'latitude'],
        'hp': ['hp', 'hewlett-packard', 'proliant', 'elitebook', 'zbook'],
        'ibm': ['ibm', 'lenovo', 'thinkcentre', 'thinkstation', 'system x'],
        'cisco': ['cisco', 'ucs'],
        'supermicro': ['supermicro', 'super micro'],
        'intel': ['intel'],
        'amd': ['amd'],
    }
    
    # Form factor patterns
    FORM_FACTORS = {
        'rackmount': r'rack(?:mount)?|rack\s*mount|1[uU]|2[uU]|3[uU]|4[uU]',
        'tower': r'tower|twer',
        'desktop': r'desktop|small\s*form\s*factor|sff\s*(?!drive|bay|slot)',
        'laptop': r'laptop|notebook',
        'blade': r'blade',
    }
    
    # CPU manufacturers
    CPU_MANUFACTURERS = ['intel', 'amd', 'ibm', 'motorola', 'sun', 'sparc']
    
    # Common CPU models
    CPU_MODELS = {
        'pentium': r'pentium\s*(?:iii?|4|d|[234])?\s*',
        'xeon': r'xeon\s*',
        'core': r'core\s*(?:i3|i5|i7|i9|2|duo|quad)?\s*',
        'athlon': r'athlon\s*',
        'opteron': r'opteron\s*',
        'ryzen': r'ryzen\s*',
        'epyc': r'epyc\s*',
    }
    
    # RAM types
    RAM_TYPES = ['ddr', 'ddr2', 'ddr3', 'ddr4', 'ddr5', 'sdram', 'rdimm', 'udimm', 'ecc']
    
    # Drive types
    DRIVE_TYPES = ['hdd', 'ssd', 'nvme', 'sas', 'sata', 'scsi', 'ide', 'flash']
    
    # Storage capacities (in GB)
    STORAGE_PATTERNS = [
        (r'(\d+(?:\.\d+)?)\s*tb', 1024),  # TB to GB
        (r'(\d+(?:\.\d+)?)\s*gb', 1),
        (r'(\d+(?:\.\d+)?)\s*mb', 0.001),
    ]
    
    # Speed patterns (GHz, MHz)
    SPEED_PATTERNS = [
        (r'(\d+(?:\.\d+)?)\s*ghz', 1),
        (r'(\d+(?:\.\d+)?)\s*mhz', 0.001),
    ]

    def __init__(self):
        self._compile_patterns()
    
    def _compile_patterns(self):
        """Compile regex patterns for efficiency"""
        self.speed_pattern = re.compile(
            r'|'.join([f'(?:{p})' for p, _ in self.SPEED_PATTERNS]),
            re.IGNORECASE
        )
        
    def parse(self, part_number: str, description: str, brand: str = "") -> ProductConfiguration:
        """Parse product description into structured configuration"""
        config = ProductConfiguration()
        config.original_part_number = part_number
        config.raw_description = description
        
        combined_text = f"{brand} {description} {part_number}".lower()
        combined_text = re.sub(r'\s+', ' ', combined_text).strip()
        
        # Extract manufacturer
        config.manufacturer = self._extract_manufacturer(combined_text, brand)
        
        # Extract product family and model
        config.product_family, config.model = self._extract_product_info(combined_text, config.manufacturer)
        
        # Extract form factor
        config.form_factor, config.rack_units = self._extract_form_factor(combined_text)
        
        # Extract generation
        config.generation = self._extract_generation(combined_text)
        
        # Extract CPU information
        config.cpu = self._extract_cpu_info(combined_text)
        
        # Extract RAM information
        config.ram = self._extract_ram_info(combined_text)
        
        # Extract drive information
        config.drives = self._extract_drive_info(combined_text)
        
        # Extract PSU information
        config.psu = self._extract_psu_info(combined_text)
        
        # Extract network information
        config.network = self._extract_network_info(combined_text)
        
        # Extract RAID controller
        config.raid_controller = self._extract_raid_controller(combined_text)
        
        return config
    
    def _extract_manufacturer(self, text: str, brand: str) -> str:
        """Extract manufacturer from text"""
        if brand:
            brand_lower = brand.lower()
            for manu, keywords in self.MANUFACTURERS.items():
                if brand_lower in keywords or manu in brand_lower:
                    return manu.upper()
        
        for manu, keywords in self.MANUFACTURERS.items():
            for keyword in keywords:
                if keyword in text:
                    return manu.upper()
        
        return ""
    
    def _extract_product_info(self, text: str, manufacturer: str) -> Tuple[str, str]:
        """Extract product family and model"""
        product_family = ""
        model = ""
        
        # Dell PowerEdge patterns
        if manufacturer == 'DELL':
            pe_match = re.search(r'power\s*edge\s*(\d+)', text, re.IGNORECASE)
            if pe_match:
                product_family = "PowerEdge"
                model = pe_match.group(1)
            
            # Also check for simpler patterns
            if not product_family:
                pe_match = re.search(r'pe\s*(\d+)', text, re.IGNORECASE)
                if pe_match:
                    product_family = "PowerEdge"
                    model = pe_match.group(1)
        
        # HP ProLiant patterns
        elif manufacturer == 'HP':
            pl_match = re.search(r'pro\s*liant\s*([a-z]*\d+[a-z]*)', text, re.IGNORECASE)
            if pl_match:
                product_family = "ProLiant"
                model = pl_match.group(1)
        
        # IBM/Lenovo patterns
        elif manufacturer in ['IBM', 'LENOVO']:
            sys_match = re.search(r'system\s*x?\s*(\d+)', text, re.IGNORECASE)
            if sys_match:
                product_family = "System x"
                model = sys_match.group(1)
        
        # Cisco UCS patterns
        elif manufacturer == 'CISCO':
            ucs_match = re.search(r'ucs\s*([a-z]*\d+)', text, re.IGNORECASE)
            if ucs_match:
                product_family = "UCS"
                model = ucs_match.group(1)
        
        # Generic model extraction
        if not model:
            # Look for alphanumeric model patterns after manufacturer
            model_match = re.search(r'(?:[a-z]+\s*)?([a-z]?\d+[a-z]*(?:\s*[a-z]\d+)*)', text, re.IGNORECASE)
            if model_match:
                model = model_match.group(1)
        
        return product_family, model
    
    def _extract_form_factor(self, text: str) -> Tuple[str, Optional[int]]:
        """Extract form factor and rack units"""
        form_factor = ""
        rack_units = None
        
        for ff, pattern in self.FORM_FACTORS.items():
            if re.search(pattern, text, re.IGNORECASE):
                form_factor = ff
                break
        
        # Extract rack units
        ru_match = re.search(r'(\d+)\s*u\b', text, re.IGNORECASE)
        if ru_match:
            rack_units = int(ru_match.group(1))
            if not form_factor:
                form_factor = "rackmount"
        
        return form_factor, rack_units
    
    def _extract_generation(self, text: str) -> str:
        """Extract generation information"""
        gen_patterns = [
            r'gen\s*(\d+)',
            r'generation\s*(\d+)',
            r'g(\d+)(?:en)?\b',
        ]
        
        for pattern in gen_patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return f"Gen{match.group(1)}"
        
        return ""
    
    def _extract_cpu_info(self, text: str) -> CPURequirement:
        """Extract CPU information from text"""
        cpu = CPURequirement()
        
        # Check for CPU manufacturer
        for manu in self.CPU_MANUFACTURERS:
            if manu in text:
                cpu.manufacturer = manu.upper()
                break
        
        # Check for CPU model
        for model_name, pattern in self.CPU_MODELS.items():
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                cpu.model = model_name
                break
        
        # Extract CPU speed
        speed_match = re.search(r'(\d+(?:\.\d+)?)\s*(?:ghz|mhz)', text, re.IGNORECASE)
        if speed_match:
            speed_val = float(speed_match.group(1))
            if 'ghz' in text[max(0, speed_match.start()-10):speed_match.end()+5].lower():
                cpu.speed_ghz = speed_val
            else:
                cpu.speed_ghz = speed_val * 0.001  # MHz to GHz
        
        # Extract CPU quantity - be careful about configuration numbers
        # Look for explicit quantity indicators
        qty_patterns = [
            r'(\d+)\s*x\s*(?:cpu|processor|intel|amd|xeon|pentium)',
            r'(?:cpu|processor)\s*x\s*(\d+)',
            r'(\d+)\s*(?:cpu|processor)s?\s+(?:with|and|@)',
            r'dual\s+(?:cpu|processor)',
            r'quad\s+(?:cpu|processor)',
        ]
        
        for pattern in qty_patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                if 'dual' in pattern:
                    cpu.quantity = 2
                elif 'quad' in pattern:
                    cpu.quantity = 4
                else:
                    cpu.quantity = int(match.group(1))
                break
        
        return cpu
    
    def _extract_ram_info(self, text: str) -> RAMRequirement:
        """Extract RAM information from text"""
        ram = RAMRequirement()
        
        # Check for RAM type
        for ram_type in self.RAM_TYPES:
            if ram_type in text.lower():
                ram.ram_type = ram_type.upper()
                break
        
        # Extract RAM capacity
        ram_match = re.search(r'(\d+(?:\.\d+)?)\s*gb\s*(?:ram|memory|ddr)?', text, re.IGNORECASE)
        if ram_match:
            ram.capacity_gb = float(ram_match.group(1))
        
        # Check for form factor
        if 'dimm' in text.lower():
            ram.form_factor = 'DIMM'
        elif 'sodimm' in text.lower():
            ram.form_factor = 'SODIMM'
        
        return ram
    
    def _extract_drive_info(self, text: str) -> List[DriveRequirement]:
        """Extract drive/storage information from text"""
        drives = []
        
        # IMPORTANT: Distinguish between drive bay count and actual drives
        # "16x SFF" usually means 16 bays, not 16 drives included
        
        # Look for explicit drive mentions
        drive_patterns = [
            r'(\d+(?:\.\d+)?)\s*tb\s*(?:hdd|ssd|drive|disk)?',
            r'(\d+(?:\.\d+)?)\s*gb\s*(?:hdd|ssd|drive|disk)?',
            r'(\d+)\s*x\s*(?:hdd|ssd|drive|disk)',
        ]
        
        for pattern in drive_patterns:
            matches = re.finditer(pattern, text, re.IGNORECASE)
            for match in matches:
                drive = DriveRequirement()
                
                # Determine capacity
                cap_match = re.search(r'(\d+(?:\.\d+)?)', match.group(0))
                if cap_match:
                    cap_val = float(cap_match.group(1))
                    if 'tb' in match.group(0).lower():
                        drive.capacity_gb = cap_val * 1024
                    else:
                        drive.capacity_gb = cap_val
                
                # Determine drive type
                if 'ssd' in match.group(0).lower():
                    drive.drive_type = 'SSD'
                elif 'hdd' in match.group(0).lower():
                    drive.drive_type = 'HDD'
                elif 'nvme' in match.group(0).lower():
                    drive.drive_type = 'NVMe'
                else:
                    drive.drive_type = 'HDD'  # Default
                
                # Determine form factor
                if 'sff' in text.lower() or '2.5' in match.group(0):
                    drive.form_factor = 'SFF (2.5")'
                elif 'lff' in text.lower() or '3.5' in match.group(0):
                    drive.form_factor = 'LFF (3.5")'
                
                drive.quantity = 1
                drives.append(drive)
        
        # Check for bay configuration (not included drives)
        bay_match = re.search(r'(\d+)\s*x\s*(sff|lff)\s*(?:bay|slot|drive\s*bay)', text, re.IGNORECASE)
        if bay_match and not drives:
            # This indicates bay count, not included drives
            drive = DriveRequirement()
            drive.form_factor = bay_match.group(2).upper()
            drive.quantity = 0  # No drives included, just bays
            drives.append(drive)
        
        return drives
    
    def _extract_psu_info(self, text: str) -> PSURequirement:
        """Extract PSU information from text"""
        psu = PSURequirement()
        
        # Look for wattage
        watt_match = re.search(r'(\d+)\s*w(?:att)?(?:s)?', text, re.IGNORECASE)
        if watt_match:
            psu.wattage = int(watt_match.group(1))
        
        # Check for redundant
        if 'redundant' in text.lower() or re.search(r'dual\s*psu', text.lower()):
            psu.redundant = True
            psu.quantity = 2
        
        return psu
    
    def _extract_network_info(self, text: str) -> NetworkRequirement:
        """Extract network information from text"""
        network = NetworkRequirement()
        
        # Look for network speed
        speed_match = re.search(r'(\d+)\s*gb(?:e)?(?:nic|port|network)?', text, re.IGNORECASE)
        if speed_match:
            network.speed = f"{speed_match.group(1)}GbE"
            network.ports = max(network.ports, 1)
        
        # Look for port count
        port_match = re.search(r'(\d+)\s*port\s*(?:nic|network|ethernet)', text, re.IGNORECASE)
        if port_match:
            network.ports = int(port_match.group(1))
        
        return network
    
    def _extract_raid_controller(self, text: str) -> str:
        """Extract RAID controller information"""
        raid_patterns = [
            r'raid\s*(\d+)?(?:controller)?',
            r'(?:perc|smart\s*array|serveRAID)\s*([a-z0-9]+)',
        ]
        
        for pattern in raid_patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return match.group(0)
        
        return ""


class eBayAPIClient:
    """Client for eBay Browse API"""
    
    MARKETPLACE_IDS = {
        'AU': 'EBAY_AU',
        'US': 'EBAY_US',
    }
    
    CURRENCIES = {
        'AU': 'AUD',
        'US': 'USD',
    }
    
    def __init__(self, client_id: str, client_secret: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self.access_token = None
        self.token_expiry = 0
        self.rate_limit_state = {
            'AU': {'limited': False, 'retry_after': 0, 'backoff': 1},
            'US': {'limited': False, 'retry_after': 0, 'backoff': 1},
        }
        self.search_cache = {}
        self._base_url = "https://api.ebay.com"
        self._oauth_url = "https://api.ebay.com/identity/v1/oauth2/token"
    
    def get_access_token(self) -> Optional[str]:
        """Get OAuth access token"""
        if self.access_token and time.time() < self.token_expiry:
            return self.access_token
        
        try:
            response = requests.post(
                self._oauth_url,
                headers={
                    'Content-Type': 'application/x-www-form-urlencoded',
                },
                data={
                    'grant_type': 'client_credentials',
                    'scope': 'https://api.ebay.com/oauth/api_scope',
                },
                auth=(self.client_id, self.client_secret),
                timeout=30
            )
            
            if response.status_code == 200:
                token_data = response.json()
                self.access_token = token_data['access_token']
                self.token_expiry = time.time() + token_data['expires_in'] - 60
                return self.access_token
            else:
                print(f"Token request failed: {response.status_code}")
                return None
                
        except Exception as e:
            print(f"Error getting access token: {e}")
            return None
    
    def _handle_rate_limit(self, marketplace: str, response: requests.Response) -> bool:
        """Handle rate limiting with exponential backoff"""
        if response.status_code == 429:
            state = self.rate_limit_state[marketplace]
            state['limited'] = True
            
            retry_after = response.headers.get('Retry-After')
            if retry_after:
                state['retry_after'] = time.time() + int(retry_after)
            else:
                state['retry_after'] = time.time() + (state['backoff'] * 2)
            
            state['backoff'] = min(state['backoff'] * 2, 60)  # Max 60 seconds
            state['backoff'] += random.uniform(0.5, 1.5)  # Add jitter
            
            return True
        
        # Reset rate limit state on successful request
        if response.status_code < 400:
            self.rate_limit_state[marketplace]['limited'] = False
            self.rate_limit_state[marketplace]['backoff'] = 1
        
        return False
    
    def _is_rate_limited(self, marketplace: str) -> bool:
        """Check if marketplace is currently rate limited"""
        state = self.rate_limit_state[marketplace]
        if state['limited'] and time.time() < state['retry_after']:
            return True
        return False
    
    def search_items(self, query: str, marketplace: str, limit: int = 50) -> Optional[List[Dict]]:
        """Search eBay items using Browse API"""
        if marketplace not in self.MARKETPLACE_IDS:
            return None
        
        # Check cache
        cache_key = f"{marketplace}:{query}"
        if cache_key in self.search_cache:
            return self.search_cache[cache_key]
        
        # Check rate limit
        if self._is_rate_limited(marketplace):
            return None
        
        token = self.get_access_token()
        if not token:
            return None
        
        headers = {
            'Authorization': f'Bearer {token}',
            'X-EBAY-C-MARKETPLACE-ID': self.MARKETPLACE_IDS[marketplace],
            'Content-Type': 'application/json',
        }
        
        params = {
            'q': query,
            'limit': min(limit, 200),
            'offset': 0,
        }
        
        try:
            response = requests.get(
                f"{self._base_url}/buy/browse/v1/item_summary/search",
                headers=headers,
                params=params,
                timeout=30
            )
            
            if self._handle_rate_limit(marketplace, response):
                return None
            
            if response.status_code == 200:
                data = response.json()
                items = data.get('itemSummaries', [])
                self.search_cache[cache_key] = items
                return items
            else:
                print(f"Search failed for {marketplace}: {response.status_code}")
                return None
                
        except requests.exceptions.Timeout:
            print(f"Request timeout for {marketplace}")
            return None
        except Exception as e:
            print(f"Error searching {marketplace}: {e}")
            return None
    
    def get_item(self, item_id: str, marketplace: str) -> Optional[Dict]:
        """Get detailed item information"""
        if marketplace not in self.MARKETPLACE_IDS:
            return None
        
        # Check rate limit
        if self._is_rate_limited(marketplace):
            return None
        
        token = self.get_access_token()
        if not token:
            return None
        
        headers = {
            'Authorization': f'Bearer {token}',
            'X-EBAY-C-MARKETPLACE-ID': self.MARKETPLACE_IDS[marketplace],
            'Content-Type': 'application/json',
        }
        
        try:
            response = requests.get(
                f"{self._base_url}/buy/browse/v1/item/{item_id}",
                headers=headers,
                timeout=30
            )
            
            if self._handle_rate_limit(marketplace, response):
                return None
            
            if response.status_code == 200:
                return response.json()
            else:
                return None
                
        except Exception as e:
            print(f"Error getting item details: {e}")
            return None


class MatchingEngine:
    """Matches eBay listings against product configurations"""
    
    def __init__(self, parser: ProductDescriptionParser):
        self.parser = parser
    
    def calculate_match_score(self, listing_title: str, config: ProductConfiguration) -> Tuple[float, List[str]]:
        """Calculate match score between listing and configuration"""
        score = 0.0
        matched_attrs = []
        title_lower = listing_title.lower()
        
        # Manufacturer match (high weight)
        if config.manufacturer and config.manufacturer.lower() in title_lower:
            score += 25
            matched_attrs.append(f"Manufacturer: {config.manufacturer}")
        
        # Product family match (high weight)
        if config.product_family:
            family_lower = config.product_family.lower()
            if family_lower in title_lower:
                score += 20
                matched_attrs.append(f"Product Family: {config.product_family}")
        
        # Model match (very high weight)
        if config.model:
            # Look for exact model number
            model_pattern = r'\b' + re.escape(config.model) + r'\b'
            if re.search(model_pattern, title_lower):
                score += 30
                matched_attrs.append(f"Model: {config.model}")
            elif config.model in title_lower:
                score += 15
                matched_attrs.append(f"Model (partial): {config.model}")
        
        # CPU manufacturer match
        if config.cpu.manufacturer and config.cpu.manufacturer.lower() in title_lower:
            score += 10
            matched_attrs.append(f"CPU: {config.cpu.manufacturer}")
        
        # CPU model match
        if config.cpu.model:
            cpu_model_lower = config.cpu.model.lower()
            if cpu_model_lower in title_lower:
                score += 15
                matched_attrs.append(f"CPU Model: {config.cpu.model}")
        
        # CPU speed match
        if config.cpu.speed_ghz:
            speed_str = f"{config.cpu.speed_ghz}ghz"
            if speed_str in title_lower:
                score += 10
                matched_attrs.append(f"CPU Speed: {config.cpu.speed_ghz}GHz")
        
        # CPU quantity match
        if config.cpu.quantity > 1:
            qty_indicators = {
                2: ['dual', '2 x', '2x', 'double'],
                4: ['quad', '4 x', '4x', 'quadruple'],
            }
            for indicator in qty_indicators.get(config.cpu.quantity, []):
                if indicator in title_lower:
                    score += 10
                    matched_attrs.append(f"CPU Qty: {config.cpu.quantity}")
                    break
        
        # RAM match
        if config.ram.capacity_gb:
            ram_str = f"{int(config.ram.capacity_gb)}gb"
            if ram_str in title_lower:
                score += 10
                matched_attrs.append(f"RAM: {config.ram.capacity_gb}GB")
        
        # Form factor match
        if config.form_factor:
            if config.form_factor.lower() in title_lower:
                score += 5
                matched_attrs.append(f"Form Factor: {config.form_factor}")
        
        # Rack units match
        if config.rack_units:
            ru_str = f"{config.rack_units}u"
            if ru_str in title_lower:
                score += 5
                matched_attrs.append(f"Rack Units: {config.rack_units}")
        
        # Part number similarity (supporting, not required)
        if config.original_part_number:
            pn_lower = config.original_part_number.lower()
            if pn_lower in title_lower:
                score += 8
                matched_attrs.append(f"Part Number: {config.original_part_number}")
            else:
                # Fuzzy match - check for significant overlap
                pn_parts = re.split(r'[-_\s]+', config.original_part_number)
                matching_parts = sum(1 for part in pn_parts if len(part) > 2 and part.lower() in title_lower)
                if matching_parts >= len(pn_parts) * 0.5:
                    score += 4
                    matched_attrs.append(f"Part Number (partial)")
        
        # Penalty for unwanted terms
        unwanted_terms = ['parts', 'broken', 'faulty', 'repair', 'spares', 'manual', 'only', 
                         'case', 'chassis only', 'empty', 'no cpu', 'no ram']
        for term in unwanted_terms:
            if term in title_lower:
                score -= 15
        
        return max(0, score), matched_attrs
    
    def is_valid_listing(self, listing_title: str, config: ProductConfiguration, 
                         min_score: float = 20) -> Tuple[bool, str]:
        """Determine if listing is valid for the configuration"""
        score, matched_attrs = self.calculate_match_score(listing_title, config)
        
        if score < min_score:
            return False, f"Low match score: {score}"
        
        title_lower = listing_title.lower()
        
        # Reject obvious non-matches
        if any(term in title_lower for term in ['manual', 'documentation', 'cd', ' dvd ']):
            return False, "Manual/documentation only"
        
        if 'parts' in title_lower and 'for' in title_lower:
            # Could be legitimate parts, check further
            pass
        
        # Check critical requirements
        if config.model:
            # Model should be present or strongly implied
            if config.model not in title_lower and config.product_family not in title_lower:
                return False, "Model/product family not found"
        
        return True, f"Valid match (score: {score})"
    
    def extract_bundle_quantity(self, title: str) -> int:
        """Extract bundle/pack quantity from title"""
        title_lower = title.lower()
        
        # Look for pack/bundle indicators
        pack_patterns = [
            r'(\d+)\s*pack',
            r'(\d+)\s*x\s*\d+\s*(?:pack|lot|bundle)',
            r'lot\s*of\s*(\d+)',
            r'bundle\s*of\s*(\d+)',
            r'set\s*of\s*(\d+)',
            r'(\d+)\s*pcs?',
            r'(\d+)\s*pieces?',
        ]
        
        for pattern in pack_patterns:
            match = re.search(pattern, title_lower)
            if match:
                return int(match.group(1))
        
        return 1
    
    def create_configuration_summary(self, config: ProductConfiguration) -> str:
        """Create human-readable configuration summary"""
        parts = []
        
        if config.manufacturer:
            parts.append(config.manufacturer)
        if config.product_family:
            parts.append(config.product_family)
        if config.model:
            parts.append(config.model)
        
        config_parts = []
        if config.cpu.quantity > 0:
            cpu_str = f"{config.cpu.quantity}x " if config.cpu.quantity > 1 else ""
            if config.cpu.model:
                cpu_str += config.cpu.model
            if config.cpu.speed_ghz:
                cpu_str += f" {config.cpu.speed_ghz}GHz"
            if cpu_str.strip():
                config_parts.append(cpu_str.strip())
        
        if config.ram.capacity_gb:
            config_parts.append(f"{int(config.ram.capacity_gb)}GB RAM")
        
        if config.drives:
            for drive in config.drives:
                if drive.capacity_gb:
                    config_parts.append(f"{drive.capacity_gb}GB {drive.drive_type}")
        
        if config_parts:
            parts.append(" | ".join(config_parts))
        
        return " ".join(parts)


class ComponentPricer:
    """Handles component-based pricing when complete configuration not available"""
    
    def __init__(self, api_client: eBayAPIClient, matching_engine: MatchingEngine):
        self.api_client = api_client
        self.matching_engine = matching_engine
    
    def find_components(self, config: ProductConfiguration, marketplace: str, 
                       max_results: int = 10) -> Dict[str, List[eBayListing]]:
        """Find individual components for building the configuration"""
        components = {}
        
        # Search for base system/server
        base_queries = self._generate_base_queries(config)
        for query in base_queries:
            listings = self._search_and_filter(query, config, marketplace, max_results // 2)
            if listings:
                components['base'] = listings
                break
        
        # Search for CPUs if needed
        if config.cpu.manufacturer and config.cpu.model:
            cpu_queries = self._generate_cpu_queries(config)
            for query in cpu_queries:
                listings = self._search_cpus(query, config, marketplace, max_results)
                if listings:
                    components['cpu'] = listings
                    break
        
        # Search for RAM if needed
        if config.ram.capacity_gb or config.ram.ram_type:
            ram_queries = self._generate_ram_queries(config)
            for query in ram_queries:
                listings = self._search_ram(query, config, marketplace, max_results)
                if listings:
                    components['ram'] = listings
                    break
        
        return components
    
    def _generate_base_queries(self, config: ProductConfiguration) -> List[str]:
        """Generate search queries for base system"""
        queries = []
        
        # Most specific first
        if config.manufacturer and config.product_family and config.model:
            queries.append(f"{config.manufacturer} {config.product_family} {config.model}")
        
        if config.manufacturer and config.model:
            queries.append(f"{config.manufacturer} {config.model}")
        
        if config.product_family and config.model:
            queries.append(f"{config.product_family} {config.model}")
        
        if config.model:
            queries.append(f"{config.model} server")
        
        return queries
    
    def _generate_cpu_queries(self, config: ProductConfiguration) -> List[str]:
        """Generate search queries for CPUs"""
        queries = []
        
        if config.cpu.model and config.cpu.speed_ghz:
            queries.append(f"{config.cpu.model} {config.cpu.speed_ghz}GHz")
        
        if config.cpu.model:
            queries.append(f"{config.cpu.model} cpu")
            queries.append(f"{config.cpu.model} processor")
        
        if config.cpu.manufacturer and config.cpu.speed_ghz:
            queries.append(f"{config.cpu.manufacturer} {config.cpu.speed_ghz}GHz")
        
        return queries
    
    def _generate_ram_queries(self, config: ProductConfiguration) -> List[str]:
        """Generate search queries for RAM"""
        queries = []
        
        if config.ram.capacity_gb and config.ram.ram_type:
            queries.append(f"{int(config.ram.capacity_gb)}GB {config.ram.ram_type}")
        
        if config.ram.capacity_gb:
            queries.append(f"{int(config.ram.capacity_gb)}GB RAM")
            queries.append(f"{int(config.ram.capacity_gb)}GB memory")
        
        if config.ram.ram_type:
            queries.append(f"{config.ram.ram_type} DIMM")
        
        return queries
    
    def _search_and_filter(self, query: str, config: ProductConfiguration, 
                          marketplace: str, limit: int) -> List[eBayListing]:
        """Search and filter results for base system"""
        items = self.api_client.search_items(query, marketplace, limit)
        if not items:
            return []
        
        listings = []
        for item in items:
            title = item.get('title', '')
            price_info = item.get('price', {})
            
            # Skip if no price
            if not price_info or 'value' not in price_info:
                continue
            
            is_valid, _ = self.matching_engine.is_valid_listing(title, config, min_score=15)
            if not is_valid:
                continue
            
            listing = self._create_listing(item, config)
            if listing:
                listings.append(listing)
        
        return sorted(listings, key=lambda x: x.total_price)[:limit]
    
    def _search_cpus(self, query: str, config: ProductConfiguration,
                    marketplace: str, limit: int) -> List[eBayListing]:
        """Search for CPUs"""
        items = self.api_client.search_items(query, marketplace, limit)
        if not items:
            return []
        
        listings = []
        for item in items:
            title = item.get('title', '').lower()
            
            # Must be actual CPU, not accessories
            if any(term in title for term in ['cooler', 'fan', 'heatsink', 'socket', 'adapter']):
                continue
            
            price_info = item.get('price', {})
            if not price_info or 'value' not in price_info:
                continue
            
            listing = self._create_listing(item, config, is_component=True)
            if listing:
                listings.append(listing)
        
        return sorted(listings, key=lambda x: x.total_price / x.bundle_quantity)[:limit]
    
    def _search_ram(self, query: str, config: ProductConfiguration,
                   marketplace: str, limit: int) -> List[eBayListing]:
        """Search for RAM"""
        items = self.api_client.search_items(query, marketplace, limit)
        if not items:
            return []
        
        listings = []
        for item in items:
            title = item.get('title', '').lower()
            
            # Must be actual RAM
            if any(term in title for term in ['slot', 'socket', 'adapter']):
                continue
            
            price_info = item.get('price', {})
            if not price_info or 'value' not in price_info:
                continue
            
            listing = self._create_listing(item, config, is_component=True)
            if listing:
                listings.append(listing)
        
        return sorted(listings, key=lambda x: x.total_price / x.bundle_quantity)[:limit]
    
    def _create_listing(self, item: Dict, config: ProductConfiguration, 
                       is_component: bool = False) -> Optional[eBayListing]:
        """Create eBayListing from API response"""
        try:
            item_id = item.get('itemId', '')
            title = item.get('title', '')
            
            price_info = item.get('price', {})
            item_price = price_info.get('value', 0) if price_info else 0
            currency = price_info.get('currency', 'USD')
            
            # Get shipping cost
            shipping_info = item.get('shippingOptions', [])
            shipping_cost = 0
            if shipping_info:
                for option in shipping_info:
                    ship_price = option.get('shippingCost', {})
                    if ship_price and 'value' in ship_price:
                        shipping_cost = ship_price['value']
                        break
            
            total_price = item_price + shipping_cost
            
            # Get item URL
            web_url = item.get('itemWebUrl', '')
            if not web_url and item_id:
                marketplace = 'ebay.com'  # Default
                web_url = f"https://www.{marketplace}/itm/{item_id}"
            
            # Calculate match score
            score, matched_attrs = self.matching_engine.calculate_match_score(title, config)
            
            # Extract bundle quantity
            bundle_qty = self.matching_engine.extract_bundle_quantity(title)
            
            # Create configuration summary
            config_summary = title[:100] + "..." if len(title) > 100 else title
            
            return eBayListing(
                item_id=item_id,
                title=title,
                item_price=item_price,
                shipping_cost=shipping_cost,
                total_price=total_price,
                currency=currency,
                url=web_url,
                condition=item.get('condition', ''),
                bundle_quantity=bundle_qty,
                match_score=score,
                matched_attributes=matched_attrs,
                configuration_summary=config_summary
            )
        except Exception as e:
            print(f"Error creating listing: {e}")
            return None
    
    def calculate_component_build_cost(self, components: Dict[str, List[eBayListing]], 
                                       config: ProductConfiguration) -> Optional[Tuple[float, str, List[eBayListing]]]:
        """Calculate total cost for component build"""
        if not components:
            return None
        
        selected_components = []
        total_cost = 0
        summary_parts = []
        
        # Base system
        if 'base' in components and components['base']:
            base = components['base'][0]
            total_cost += base.total_price
            selected_components.append(base)
            summary_parts.append(f"Base: {base.title[:50]}")
        
        # CPUs
        if 'cpu' in components and components['cpu'] and config.cpu.quantity > 0:
            cpu_listings = components['cpu']
            needed = config.cpu.quantity
            
            # Find best combination considering bundles
            cpu_cost = 0
            cpu_selected = []
            
            for listing in sorted(cpu_listings, key=lambda x: x.total_price / x.bundle_quantity):
                if needed <= 0:
                    break
                
                bundle_qty = listing.bundle_quantity
                if bundle_qty >= needed:
                    # One purchase covers all needed
                    cpu_cost += listing.total_price
                    cpu_selected.append(listing)
                    summary_parts.append(f"CPU × {needed}: {listing.title[:40]}")
                    needed = 0
                else:
                    # Use this bundle and continue
                    cpu_cost += listing.total_price
                    cpu_selected.append(listing)
                    summary_parts.append(f"CPU × {bundle_qty}: {listing.title[:40]}")
                    needed -= bundle_qty
            
            if needed > 0:
                # Couldn't fulfill CPU requirement
                return None
            
            total_cost += cpu_cost
            selected_components.extend(cpu_selected)
        
        # RAM
        if 'ram' in components and components['ram'] and config.ram.capacity_gb:
            ram_listings = components['ram']
            
            # Find compatible RAM
            for listing in sorted(ram_listings, key=lambda x: x.total_price / x.bundle_quantity):
                total_cost += listing.total_price
                selected_components.append(listing)
                summary_parts.append(f"RAM: {listing.title[:40]}")
                break
        
        if not selected_components:
            return None
        
        summary = " | ".join(summary_parts)
        return total_cost, summary, selected_components


class eBayPricingEngine:
    """Main pricing engine orchestrating the entire process"""
    
    def __init__(self, client_id: str, client_secret: str):
        self.api_client = eBayAPIClient(client_id, client_secret)
        self.parser = ProductDescriptionParser()
        self.matching_engine = MatchingEngine(self.parser)
        self.component_pricer = ComponentPricer(self.api_client, self.matching_engine)
        self.checkpoint_file = "ebay_pricing_checkpoint.csv"
        self.results_cache = {}
    
    def process_row(self, row: Dict, marketplaces: List[str] = ['AU', 'US']) -> Dict:
        """Process a single CSV row"""
        result = {**row}  # Preserve original columns
        
        # Extract relevant fields
        part_number = self._safe_get(row, ['Part Number', 'PartNumber', 'Part_No', 'PN'])
        description = self._safe_get(row, ['Product Description', 'Description', 'Desc', 'Product_Desc'])
        brand = self._safe_get(row, ['Brand', 'Manufacturer', 'Mfg'])
        
        if not description:
            result['Status'] = Status.ERROR.value
            result['Error'] = "Missing product description"
            return result
        
        # Parse configuration
        config = self.parser.parse(part_number or "", description, brand or "")
        
        # Check if configuration is too incomplete
        if not config.manufacturer and not config.model and not config.product_family:
            result['Status'] = Status.INCOMPLETE_CONFIGURATION.value
            result['Error'] = "Could not parse product configuration"
            return result
        
        # Process each marketplace
        for marketplace in marketplaces:
            prefix = f"AU" if marketplace == 'AU' else "US"
            
            search_result = self._process_marketplace(config, marketplace)
            
            # Merge results
            output_dict = search_result.to_output_dict(prefix)
            result.update(output_dict)
        
        result['Status'] = self._determine_overall_status(result)
        
        return result
    
    def _safe_get(self, row: Dict, keys: List[str]) -> str:
        """Safely get value from row with multiple possible keys"""
        for key in keys:
            # Try exact match
            if key in row:
                val = row[key]
                return str(val) if val is not None else ""
            
            # Try case-insensitive match
            for row_key in row.keys():
                if row_key.lower().strip() == key.lower().strip():
                    val = row[row_key]
                    return str(val) if val is not None else ""
        
        return ""
    
    def _process_marketplace(self, config: ProductConfiguration, marketplace: str) -> SearchResult:
        """Process a single marketplace"""
        # Check rate limit
        if self.api_client._is_rate_limited(marketplace):
            return SearchResult(
                status=Status.RATE_LIMITED,
                match_type=MatchType.RATE_LIMITED,
                error_message=f"Rate limited for {marketplace}"
            )
        
        # Generate search queries from configuration
        queries = self._generate_search_queries(config)
        
        all_listings = []
        
        # Search with each query
        for query in queries:
            if self.api_client._is_rate_limited(marketplace):
                break
            
            items = self.api_client.search_items(query, marketplace, limit=50)
            if not items:
                continue
            
            for item in items:
                title = item.get('title', '')
                is_valid, _ = self.matching_engine.is_valid_listing(title, config)
                if not is_valid:
                    continue
                
                listing = self.component_pricer._create_listing(item, config)
                if listing and listing not in all_listings:
                    all_listings.append(listing)
        
        if not all_listings:
            # Try component-based pricing
            components = self.component_pricer.find_components(config, marketplace)
            build_result = self.component_pricer.calculate_component_build_cost(components, config)
            
            if build_result:
                total_cost, summary, selected = build_result
                return SearchResult(
                    status=Status.FOUND,
                    match_type=MatchType.COMPONENT_BUILD,
                    cheapest_price=total_cost,
                    link=selected[0].url if selected else "",
                    item_price=sum(c.item_price for c in selected),
                    shipping_cost=sum(c.shipping_cost for c in selected),
                    total_price=total_cost,
                    currency=self.api_client.CURRENCIES.get(marketplace, ''),
                    configuration_summary=summary,
                    bundle_quantity=1,
                    listings=selected
                )
            
            return SearchResult(
                status=Status.NOT_FOUND,
                match_type=MatchType.NOT_FOUND
            )
        
        # Sort by total price
        all_listings.sort(key=lambda x: x.total_price)
        best = all_listings[0]
        
        # Determine match type
        match_type = MatchType.COMPLETE_LISTING
        if best.bundle_quantity > 1:
            match_type = MatchType.BUNDLE
        
        return SearchResult(
            status=Status.FOUND,
            match_type=match_type,
            cheapest_price=best.total_price,
            link=best.url,
            item_price=best.item_price,
            shipping_cost=best.shipping_cost,
            total_price=best.total_price,
            currency=best.currency,
            configuration_summary=best.configuration_summary,
            bundle_quantity=best.bundle_quantity,
            listings=all_listings[:5]  # Keep top 5
        )
    
    def _generate_search_queries(self, config: ProductConfiguration) -> List[str]:
        """Generate prioritized search queries from configuration"""
        queries = []
        
        # Most specific queries first
        if config.manufacturer and config.product_family and config.model:
            queries.append(f"{config.manufacturer} {config.product_family} {config.model}")
        
        if config.manufacturer and config.model:
            queries.append(f"{config.manufacturer} {config.model}")
        
        if config.product_family and config.model:
            queries.append(f"{config.product_family} {config.model}")
        
        # Add configuration details
        if config.model:
            base = f"{config.manufacturer or ''} {config.product_family or ''} {config.model}".strip()
            
            if config.cpu.model:
                queries.append(f"{base} {config.cpu.model}")
            
            if config.cpu.speed_ghz:
                queries.append(f"{base} {config.cpu.speed_ghz}GHz")
            
            if config.ram.capacity_gb:
                queries.append(f"{base} {int(config.ram.capacity_gb)}GB RAM")
            
            if config.cpu.model and config.cpu.speed_ghz:
                queries.append(f"{base} {config.cpu.model} {config.cpu.speed_ghz}GHz")
        
        # Fallback to model only
        if config.model and f"{config.model}" not in queries:
            queries.append(f"{config.model}")
        
        # Remove duplicates while preserving order
        seen = set()
        unique_queries = []
        for q in queries:
            q_clean = ' '.join(q.split())
            if q_clean and q_clean not in seen and len(q_clean) > 2:
                seen.add(q_clean)
                unique_queries.append(q_clean)
        
        return unique_queries[:10]  # Limit queries
    
    def _determine_overall_status(self, result: Dict) -> str:
        """Determine overall status from marketplace results"""
        au_status = result.get('AU_Status', '')
        us_status = result.get('US_Status', '')
        
        if au_status == Status.FOUND.value or us_status == Status.FOUND.value:
            return Status.FOUND.value
        elif au_status == Status.RATE_LIMITED.value or us_status == Status.RATE_LIMITED.value:
            return Status.RATE_LIMITED.value
        elif au_status == Status.ERROR.value or us_status == Status.ERROR.value:
            return Status.ERROR.value
        elif au_status == Status.INCOMPLETE_CONFIGURATION.value:
            return Status.INCOMPLETE_CONFIGURATION.value
        else:
            return Status.NOT_FOUND.value
    
    def save_checkpoint(self, df: pd.DataFrame):
        """Save progress checkpoint"""
        df.to_csv(self.checkpoint_file, index=False)
    
    def load_checkpoint(self) -> Optional[pd.DataFrame]:
        """Load checkpoint if exists"""
        if os.path.exists(self.checkpoint_file):
            return pd.read_csv(self.checkpoint_file)
        return None
    
    def process_csv(self, input_df: pd.DataFrame, batch_size: int = 10, 
                   save_interval: int = 5) -> pd.DataFrame:
        """Process entire CSV with checkpointing"""
        # Normalize column names
        input_df.columns = input_df.columns.str.strip()
        
        # Check for existing checkpoint
        checkpoint_df = self.load_checkpoint()
        start_idx = 0
        
        if checkpoint_df is not None and len(checkpoint_df) > 0:
            print(f"Found checkpoint with {len(checkpoint_df)} rows")
            # Find where to resume
            if len(checkpoint_df) < len(input_df):
                start_idx = len(checkpoint_df)
                print(f"Resuming from row {start_idx}")
            else:
                print("All rows already processed in checkpoint")
                return checkpoint_df
        
        results = []
        
        # Process in batches
        total_rows = len(input_df)
        for idx in range(start_idx, total_rows):
            row = input_df.iloc[idx]
            row_dict = row.to_dict()
            
            print(f"\nProcessing row {idx + 1}/{total_rows}")
            
            try:
                result = self.process_row(row_dict)
                results.append(result)
            except Exception as e:
                print(f"Error processing row {idx + 1}: {e}")
                result = {**row_dict, 'Status': Status.ERROR.value, 'Error': str(e)}
                results.append(result)
            
            # Save checkpoint periodically
            if (idx + 1) % save_interval == 0:
                checkpoint_df = pd.DataFrame(results)
                self.save_checkpoint(checkpoint_df)
                print(f"Checkpoint saved at row {idx + 1}")
            
            # Rate limiting pause
            time.sleep(0.5)
        
        return pd.DataFrame(results)


def main():
    """Main entry point for Google Colab"""
    print("=" * 60)
    print("eBay AU + US Configuration-Based Pricing Engine")
    print("=" * 60)
    print()
    
    if not COLAB_AVAILABLE:
        print("Warning: Not running in Google Colab. Some features may be limited.")
    
    # Step 1: Get API credentials
    print("Please enter your eBay Production API credentials:")
    print("(These are NOT stored and are used only for API authentication)")
    print()
    
    client_id = input("Client ID (App ID): ").strip()
    client_secret = input("Client Secret (Cert ID): ").strip()
    
    if not client_id or not client_secret:
        print("Error: API credentials are required")
        return
    
    # Step 2: Upload CSV
    print()
    print("Please upload your CSV file:")
    
    if COLAB_AVAILABLE:
        uploaded = files.upload()
        if not uploaded:
            print("No file uploaded")
            return
        csv_filename = list(uploaded.keys())[0]
    else:
        csv_filename = input("Enter CSV filename: ").strip()
    
    # Load CSV
    print(f"Loading {csv_filename}...")
    try:
        df = pd.read_csv(csv_filename, dtype=str)
        print(f"Loaded {len(df)} rows")
        print(f"Columns: {list(df.columns)}")
    except Exception as e:
        print(f"Error loading CSV: {e}")
        return
    
    # Step 3: Initialize engine
    print()
    print("Initializing pricing engine...")
    engine = eBayPricingEngine(client_id, client_secret)
    
    # Step 4: Process CSV
    print()
    print("Starting processing...")
    print("Progress will be saved to checkpoint file regularly")
    print()
    
    results_df = engine.process_csv(df, batch_size=10, save_interval=5)
    
    # Step 5: Export results
    print()
    print("Processing complete!")
    print(f"Results: {len(results_df)} rows")
    
    # Export to Excel
    output_filename = "ebay_pricing_results.xlsx"
    try:
        results_df.to_excel(output_filename, index=False)
        print(f"Results exported to {output_filename}")
        
        if COLAB_AVAILABLE:
            print("Downloading file...")
            files.download(output_filename)
    except Exception as e:
        print(f"Error exporting to Excel: {e}")
        # Fallback to CSV
        csv_output = "ebay_pricing_results.csv"
        results_df.to_csv(csv_output, index=False)
        print(f"Results exported to {csv_output} (CSV fallback)")
        
        if COLAB_AVAILABLE:
            files.download(csv_output)
    
    print()
    print("=" * 60)
    print("Processing complete!")
    print("=" * 60)


if __name__ == "__main__":
    main()
