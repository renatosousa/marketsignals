"""
Implementação de referência em Python do Delta de Pressão
Pode ser usada para validar a lógica MQL5 e criar testes unitários
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Tuple
import pandas as pd


@dataclass
class Tick:
    """Representa um tick de negociação"""
    time: datetime
    volume_buy: float
    volume_sell: float


class PressureDelta:
    """
    Calcula o delta de pressão de agressão tick-by-tick.
    
    Lógica:
    1. Mantém buffer de ticks dentro da janela de tempo
    2. Calcula % de volume comprador vs vendedor
    3. Delta = variação percentual em relação ao cálculo anterior
    """
    
    def __init__(self, window_seconds: int = 5):
        self.window_seconds = window_seconds
        self.ticks: List[Tick] = []
        
        self.current_buy_volume = 0.0
        self.current_sell_volume = 0.0
        self.current_buy_pct = 0.0
        self.current_sell_pct = 0.0
        
        self.prev_buy_pct = 0.0
        self.prev_sell_pct = 0.0
        
        self.delta_buy_pct = 0.0
        self.delta_sell_pct = 0.0
    
    def add_tick(self, time: datetime, volume_buy: float, volume_sell: float):
        """Adiciona um novo tick ao buffer"""
        self.ticks.append(Tick(time, volume_buy, volume_sell))
    
    def remove_old_ticks(self, current_time: datetime):
        """Remove ticks fora da janela de tempo"""
        cutoff = current_time - timedelta(seconds=self.window_seconds)
        self.ticks = [t for t in self.ticks if t.time >= cutoff]
    
    def update_percentages(self):
        """Atualiza as porcentagens de compra e venda"""
        self.current_buy_volume = sum(t.volume_buy for t in self.ticks)
        self.current_sell_volume = sum(t.volume_sell for t in self.ticks)
        
        total = self.current_buy_volume + self.current_sell_volume
        
        if total > 0:
            self.current_buy_pct = (self.current_buy_volume / total) * 100.0
            self.current_sell_pct = (self.current_sell_volume / total) * 100.0
        else:
            self.current_buy_pct = 0.0
            self.current_sell_pct = 0.0
    
    def calculate(self, current_time: datetime):
        """
        Calcula o delta de pressão.
        
        Args:
            current_time: Timestamp atual para filtrar a janela
        """
        self.remove_old_ticks(current_time)
        
        # Guarda valores anteriores
        self.prev_buy_pct = self.current_buy_pct
        self.prev_sell_pct = self.current_sell_pct
        
        # Atualiza valores atuais
        self.update_percentages()
        
        # Calcula delta (variação em pontos percentuais)
        self.delta_buy_pct = self.current_buy_pct - self.prev_buy_pct
        self.delta_sell_pct = self.current_sell_pct - self.prev_sell_pct
    
    def get_bias_label(self) -> str:
        """Retorna o rótulo de viés (bias)"""
        if self.current_buy_pct >= 60.0:
            return "Compra Forte"
        elif self.current_buy_pct >= 52.0:
            return "Compra Moderada"
        elif self.current_sell_pct >= 60.0:
            return "Venda Forte"
        elif self.current_sell_pct >= 52.0:
            return "Venda Moderada"
        else:
            return "Neutro"
    
    def get_state(self) -> dict:
        """Retorna o estado atual como dicionário"""
        return {
            'buy_pct': self.current_buy_pct,
            'sell_pct': self.current_sell_pct,
            'delta_buy_pct': self.delta_buy_pct,
            'delta_sell_pct': self.delta_sell_pct,
            'buy_volume': self.current_buy_volume,
            'sell_volume': self.current_sell_volume,
            'bias': self.get_bias_label(),
            'tick_count': len(self.ticks)
        }


def process_tick_data(df: pd.DataFrame, window_seconds: int = 5) -> pd.DataFrame:
    """
    Processa um DataFrame de ticks e calcula o delta de pressão.
    
    Args:
        df: DataFrame com colunas ['time', 'volume_buy', 'volume_sell']
        window_seconds: Janela de tempo em segundos
    
    Returns:
        DataFrame com métricas de pressão calculadas
    """
    pressure = PressureDelta(window_seconds)
    results = []
    
    for _, row in df.iterrows():
        pressure.add_tick(row['time'], row['volume_buy'], row['volume_sell'])
        pressure.calculate(row['time'])
        
        state = pressure.get_state()
        state['time'] = row['time']
        results.append(state)
    
    return pd.DataFrame(results)


def generate_signal(state: dict, trigger_delta_pct: float = 10.0) -> Tuple[str, bool]:
    """
    Gera sinal de trading baseado no estado da pressão.
    
    Args:
        state: Dicionário retornado por get_state()
        trigger_delta_pct: Delta mínimo para gerar sinal
    
    Returns:
        Tupla (tipo_sinal, should_trade)
        tipo_sinal: 'BUY', 'SELL', ou 'NONE'
        should_trade: True se o delta atingiu o gatilho
    """
    delta_buy = state['delta_buy_pct']
    delta_sell = state['delta_sell_pct']
    
    if delta_buy >= trigger_delta_pct:
        return 'BUY', True
    elif delta_sell >= trigger_delta_pct:
        return 'SELL', True
    else:
        return 'NONE', False


# Exemplo de uso
if __name__ == '__main__':
    # Dados simulados
    import numpy as np
    
    np.random.seed(42)
    n_ticks = 100
    
    # Simula ticks com tendência compradora nos primeiros 50, depois vendedora
    base_time = datetime(2026, 10, 21, 10, 0, 0)
    times = [base_time + timedelta(seconds=i*0.5) for i in range(n_ticks)]
    
    # Primeiros 50: mais compra (60/40)
    # Últimos 50: mais venda (40/60)
    volumes_buy = np.concatenate([
        np.random.uniform(5, 15, 50),
        np.random.uniform(2, 8, 50)
    ])
    volumes_sell = np.concatenate([
        np.random.uniform(2, 8, 50),
        np.random.uniform(5, 15, 50)
    ])
    
    df = pd.DataFrame({
        'time': times,
        'volume_buy': volumes_buy,
        'volume_sell': volumes_sell
    })
    
    # Processa
    results = process_tick_data(df, window_seconds=5)
    
    # Exibe resultados chave
    print("=" * 70)
    print("EXEMPLO: Simulação de Delta de Pressão")
    print("=" * 70)
    print(f"\nJanela: 5 segundos | Total de ticks: {n_ticks}")
    print("\nPrimeiros 5 cálculos:")
    print(results[['time', 'buy_pct', 'sell_pct', 'delta_buy_pct', 'delta_sell_pct', 'bias']].head())
    
    print("\n\nÚltimos 5 cálculos (após mudança de regime):")
    print(results[['time', 'buy_pct', 'sell_pct', 'delta_buy_pct', 'delta_sell_pct', 'bias']].tail())
    
    # Detecta sinais
    print("\n" + "=" * 70)
    print("SINAIS DE TRADING (Delta >= 10%)")
    print("=" * 70)
    
    for idx, row in results.iterrows():
        signal, should_trade = generate_signal(row.to_dict(), trigger_delta_pct=10.0)
        if should_trade:
            print(f"\n[{row['time']}] SINAL: {signal}")
            print(f"  Compra: {row['buy_pct']:.1f}% (Δ{row['delta_buy_pct']:+.1f}%)")
            print(f"  Venda:  {row['sell_pct']:.1f}% (Δ{row['delta_sell_pct']:+.1f}%)")
            print(f"  Viés:   {row['bias']}")
    
    print("\n" + "=" * 70)
