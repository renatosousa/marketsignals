"""
Testes unitários para a implementação de referência do Delta de Pressão
"""

import pytest
from datetime import datetime, timedelta
from pressure_delta_reference import PressureDelta, Tick, generate_signal


class TestPressureDelta:
    """Testes para a classe PressureDelta"""
    
    def test_initialization(self):
        """Testa inicialização correta"""
        pressure = PressureDelta(window_seconds=5)
        assert pressure.window_seconds == 5
        assert len(pressure.ticks) == 0
        assert pressure.current_buy_pct == 0.0
        assert pressure.current_sell_pct == 0.0
    
    def test_add_tick(self):
        """Testa adição de ticks"""
        pressure = PressureDelta()
        time = datetime(2026, 10, 21, 10, 0, 0)
        
        pressure.add_tick(time, volume_buy=10.0, volume_sell=5.0)
        
        assert len(pressure.ticks) == 1
        assert pressure.ticks[0].volume_buy == 10.0
        assert pressure.ticks[0].volume_sell == 5.0
    
    def test_remove_old_ticks(self):
        """Testa remoção de ticks antigos"""
        pressure = PressureDelta(window_seconds=5)
        base_time = datetime(2026, 10, 21, 10, 0, 0)
        
        # Adiciona ticks espaçados por 1 segundo
        for i in range(10):
            pressure.add_tick(
                base_time + timedelta(seconds=i),
                volume_buy=1.0,
                volume_sell=1.0
            )
        
        assert len(pressure.ticks) == 10
        
        # Remove ticks com mais de 5 segundos do tick mais recente
        current_time = base_time + timedelta(seconds=9)
        pressure.remove_old_ticks(current_time)
        
        # Deve manter apenas ticks dos últimos 5 segundos (inclusivo)
        # current_time - 5s = t=4, então mantém t=4,5,6,7,8,9 = 6 ticks
        assert len(pressure.ticks) == 6
    
    def test_update_percentages_balanced(self):
        """Testa cálculo de porcentagens com volume balanceado"""
        pressure = PressureDelta()
        time = datetime(2026, 10, 21, 10, 0, 0)
        
        # 50/50 de compra e venda
        pressure.add_tick(time, volume_buy=10.0, volume_sell=10.0)
        pressure.update_percentages()
        
        assert pressure.current_buy_pct == 50.0
        assert pressure.current_sell_pct == 50.0
    
    def test_update_percentages_buy_dominant(self):
        """Testa cálculo com compra dominante"""
        pressure = PressureDelta()
        time = datetime(2026, 10, 21, 10, 0, 0)
        
        # 75% compra, 25% venda
        pressure.add_tick(time, volume_buy=30.0, volume_sell=10.0)
        pressure.update_percentages()
        
        assert pressure.current_buy_pct == 75.0
        assert pressure.current_sell_pct == 25.0
    
    def test_update_percentages_sell_dominant(self):
        """Testa cálculo com venda dominante"""
        pressure = PressureDelta()
        time = datetime(2026, 10, 21, 10, 0, 0)
        
        # 20% compra, 80% venda
        pressure.add_tick(time, volume_buy=10.0, volume_sell=40.0)
        pressure.update_percentages()
        
        assert pressure.current_buy_pct == 20.0
        assert pressure.current_sell_pct == 80.0
    
    def test_update_percentages_zero_volume(self):
        """Testa cálculo sem volume"""
        pressure = PressureDelta()
        pressure.update_percentages()
        
        assert pressure.current_buy_pct == 0.0
        assert pressure.current_sell_pct == 0.0
    
    def test_calculate_delta_increase(self):
        """Testa cálculo do delta com aumento de pressão compradora"""
        pressure = PressureDelta(window_seconds=10)
        base_time = datetime(2026, 10, 21, 10, 0, 0)
        
        # Primeiro: 50/50
        pressure.add_tick(base_time, volume_buy=10.0, volume_sell=10.0)
        pressure.calculate(base_time)
        
        assert pressure.current_buy_pct == 50.0
        assert pressure.delta_buy_pct == 50.0  # Subiu de 0 para 50
        
        # Segundo: 70/30 (aumento de compra)
        pressure.add_tick(base_time + timedelta(seconds=1), volume_buy=30.0, volume_sell=10.0)
        pressure.calculate(base_time + timedelta(seconds=1))
        
        # Agora temos total: 40 compra, 20 venda = 66.67% compra
        assert pressure.current_buy_pct == pytest.approx(66.67, rel=0.01)
        assert pressure.delta_buy_pct == pytest.approx(16.67, rel=0.01)
    
    def test_calculate_delta_decrease(self):
        """Testa cálculo do delta com queda de pressão compradora"""
        pressure = PressureDelta(window_seconds=10)
        base_time = datetime(2026, 10, 21, 10, 0, 0)
        
        # Primeiro: 70/30
        pressure.add_tick(base_time, volume_buy=70.0, volume_sell=30.0)
        pressure.calculate(base_time)
        
        assert pressure.current_buy_pct == 70.0
        
        # Segundo: 30/70 (queda de compra)
        pressure.add_tick(base_time + timedelta(seconds=1), volume_buy=10.0, volume_sell=50.0)
        pressure.calculate(base_time + timedelta(seconds=1))
        
        # Agora temos total: 80 compra, 80 venda = 50% compra
        assert pressure.current_buy_pct == 50.0
        assert pressure.delta_buy_pct == -20.0
    
    def test_get_bias_label_strong_buy(self):
        """Testa rótulo de viés - Compra Forte"""
        pressure = PressureDelta()
        time = datetime(2026, 10, 21, 10, 0, 0)
        
        pressure.add_tick(time, volume_buy=65.0, volume_sell=35.0)
        pressure.update_percentages()
        
        assert pressure.get_bias_label() == "Compra Forte"
    
    def test_get_bias_label_moderate_buy(self):
        """Testa rótulo de viés - Compra Moderada"""
        pressure = PressureDelta()
        time = datetime(2026, 10, 21, 10, 0, 0)
        
        pressure.add_tick(time, volume_buy=55.0, volume_sell=45.0)
        pressure.update_percentages()
        
        assert pressure.get_bias_label() == "Compra Moderada"
    
    def test_get_bias_label_strong_sell(self):
        """Testa rótulo de viés - Venda Forte"""
        pressure = PressureDelta()
        time = datetime(2026, 10, 21, 10, 0, 0)
        
        pressure.add_tick(time, volume_buy=35.0, volume_sell=65.0)
        pressure.update_percentages()
        
        assert pressure.get_bias_label() == "Venda Forte"
    
    def test_get_bias_label_moderate_sell(self):
        """Testa rótulo de viés - Venda Moderada"""
        pressure = PressureDelta()
        time = datetime(2026, 10, 21, 10, 0, 0)
        
        pressure.add_tick(time, volume_buy=45.0, volume_sell=55.0)
        pressure.update_percentages()
        
        assert pressure.get_bias_label() == "Venda Moderada"
    
    def test_get_bias_label_neutral(self):
        """Testa rótulo de viés - Neutro"""
        pressure = PressureDelta()
        time = datetime(2026, 10, 21, 10, 0, 0)
        
        pressure.add_tick(time, volume_buy=50.0, volume_sell=50.0)
        pressure.update_percentages()
        
        assert pressure.get_bias_label() == "Neutro"
    
    def test_get_state(self):
        """Testa retorno do estado completo"""
        pressure = PressureDelta()
        time = datetime(2026, 10, 21, 10, 0, 0)
        
        pressure.add_tick(time, volume_buy=60.0, volume_sell=40.0)
        pressure.calculate(time)
        
        state = pressure.get_state()
        
        assert 'buy_pct' in state
        assert 'sell_pct' in state
        assert 'delta_buy_pct' in state
        assert 'delta_sell_pct' in state
        assert 'buy_volume' in state
        assert 'sell_volume' in state
        assert 'bias' in state
        assert 'tick_count' in state
        
        assert state['buy_pct'] == 60.0
        assert state['sell_pct'] == 40.0
        assert state['bias'] == "Compra Forte"


class TestSignalGeneration:
    """Testes para geração de sinais"""
    
    def test_generate_signal_buy(self):
        """Testa geração de sinal de compra"""
        state = {
            'delta_buy_pct': 12.0,
            'delta_sell_pct': 2.0
        }
        
        signal, should_trade = generate_signal(state, trigger_delta_pct=10.0)
        
        assert signal == 'BUY'
        assert should_trade is True
    
    def test_generate_signal_sell(self):
        """Testa geração de sinal de venda"""
        state = {
            'delta_buy_pct': 2.0,
            'delta_sell_pct': 15.0
        }
        
        signal, should_trade = generate_signal(state, trigger_delta_pct=10.0)
        
        assert signal == 'SELL'
        assert should_trade is True
    
    def test_generate_signal_none(self):
        """Testa ausência de sinal"""
        state = {
            'delta_buy_pct': 5.0,
            'delta_sell_pct': 3.0
        }
        
        signal, should_trade = generate_signal(state, trigger_delta_pct=10.0)
        
        assert signal == 'NONE'
        assert should_trade is False
    
    def test_generate_signal_threshold(self):
        """Testa sinal exatamente no limiar"""
        state = {
            'delta_buy_pct': 10.0,
            'delta_sell_pct': 0.0
        }
        
        signal, should_trade = generate_signal(state, trigger_delta_pct=10.0)
        
        assert signal == 'BUY'
        assert should_trade is True


class TestWindowSliding:
    """Testes para comportamento da janela deslizante"""
    
    def test_window_slides_correctly(self):
        """Testa que a janela desliza corretamente e ticks antigos são removidos"""
        pressure = PressureDelta(window_seconds=3)
        base_time = datetime(2026, 10, 21, 10, 0, 0)
        
        # t=0: 100% compra
        pressure.add_tick(base_time, volume_buy=10.0, volume_sell=0.0)
        pressure.calculate(base_time)
        assert pressure.current_buy_pct == 100.0
        
        # t=1: ainda dentro da janela
        pressure.add_tick(base_time + timedelta(seconds=1), volume_buy=10.0, volume_sell=0.0)
        pressure.calculate(base_time + timedelta(seconds=1))
        assert pressure.current_buy_pct == 100.0
        
        # t=4: tick t=0 sai da janela (janela = 3s)
        # Adiciona 50% venda
        pressure.add_tick(base_time + timedelta(seconds=4), volume_buy=0.0, volume_sell=20.0)
        pressure.calculate(base_time + timedelta(seconds=4))
        
        # Agora só tem t=1 (10 compra) e t=4 (20 venda) = 10/(10+20) = 33.33% compra
        assert pressure.current_buy_pct == pytest.approx(33.33, rel=0.01)


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
